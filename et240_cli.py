"""Direct Windows ET240 controller with CY4500 CLI command/CSV compatibility."""
from __future__ import annotations

import argparse
from collections import Counter, deque
from contextlib import ExitStack
from datetime import datetime
import csv
import gzip
import base64
import json
import math
from pathlib import Path
import statistics
import struct
import sys
import time
from types import SimpleNamespace
import xml.etree.ElementTree as XML

from et240_usb import ET240, ET240Error, enumerate_devices
from et240_protocol import (StreamDecoder, PDPacket, ScopeSample, FramingError,
                            CSV_COLUMNS, SCOPE_COLUMNS, read_transfers, decode_bmc, ticks_to_us)
from ezpd_protocol import (decode_capture_record, decode_pdo, decode_rdo, format_pdo,
                           format_rdo, MESSAGE_TYPES_BY_CLASS)
from utility_export import UtilityExport, _packet
import et240_analysis as analysis

OUTPUT_SUFFIXES = ('.xfers.bin','.records.bin','.records.hex.txt','.records.jsonl','.csv',
                   '.scope.csv','.scope.xfers.bin','.ccgx3','.summary.txt','.metadata.json',
                   '.transitions.csv','.transitions.txt','.transition_summary.csv','.transition_summary.txt')
SOP_INDEX = {'SOP':0,'SOP_PRIME':1,'SOP_DPRIME':2,'SOP_P_DBG':3,'SOP_DP_DBG':4,
             'HARD_RESET':5,'CABLE_RESET':6}


def positive(value):
    value=float(value)
    if not math.isfinite(value) or value<=0:
        raise argparse.ArgumentTypeError('must be finite and greater than zero')
    return value


def positive_int(value):
    value=int(value)
    if value<=0: raise argparse.ArgumentTypeError('must be greater than zero')
    return value


def jsonable(value):
    if isinstance(value,bytes): return value.hex(' ')
    if isinstance(value,dict): return {k:jsonable(v) for k,v in value.items()}
    if isinstance(value,(tuple,list)): return [jsonable(v) for v in value]
    return value


def normalized_record(packet,index,origin_ticks):
    """Derived CY4500 record for interchange; NEVER an ET240 wire record."""
    start=round(ticks_to_us(packet.start_ticks-origin_ticks))
    end=round(ticks_to_us(packet.end_ticks-origin_ticks))
    voltage=round(max(0,packet.vbus_mV or 0)*4096/(4048*12))
    word=packet.header|(SOP_INDEX.get(packet.sop,7)<<16)
    if packet.ok: word|=1<<31
    elif packet.error=='CRC mismatch': word|=1<<30
    else: word|=1<<29
    return struct.pack('<5I',index,voltage,start&0xFFFFFFFF,end&0xFFFFFFFF,word)+packet.payload[2:46].ljust(44,b'\0')


class SessionOutput:
    def __init__(self,args):
        self.args=args
        self.prefix=Path(args.out_prefix)
        analysis._check_output_paths([self.prefix.with_suffix(s) for s in OUTPUT_SUFFIXES],force=args.force)
        self.prefix.parent.mkdir(parents=True,exist_ok=True)
        self.stack=ExitStack(); self.count=0; self.scope_count=0; self.previous_end=None
        self.counts=Counter(); self.errors=Counter(); self.source_pdos={}; self.origin_ticks=None
        self.paths=[]
        try:
            self.records=self.open('.records.bin','wb')
            self.hex=self.open('.records.hex.txt','w')
            self.jsonl=self.open('.records.jsonl','w')
            self.utility=UtilityExport(self.path('.csv'),self.path('.ccgx3') if args.ccgx3 else None)
            self.stack.callback(self.utility.close)
            self.paths.append(self.path('.csv'))
            if args.ccgx3: self.paths.append(self.path('.ccgx3'))
            if args.scope and getattr(args,'scope_csv_path',None):
                path=Path(args.scope_csv_path); path.parent.mkdir(parents=True,exist_ok=True)
                self.paths.append(path)
                self.scope_writer=csv.writer(self.stack.enter_context(path.open('w',encoding='utf-8',newline='')))
            else:
                self.scope_writer=csv.writer(self.open('.scope.csv','w')) if args.scope else None
            if self.scope_writer: self.scope_writer.writerow(SCOPE_COLUMNS)
            self.xfers=self.open('.xfers.bin','wb') if args.save_transfers else None
            if args.scope_raw and getattr(args,'scope_raw_path',None):
                path=Path(args.scope_raw_path);path.parent.mkdir(parents=True,exist_ok=True)
                self.paths.append(path)
                self.scope_xfers=self.stack.enter_context(path.open('wb'))
            else:
                self.scope_xfers=self.open('.scope.xfers.bin','wb') if args.scope_raw else None
        except BaseException:
            self.stack.close(); raise

    def path(self,suffix): return self.prefix.with_suffix(suffix)

    def open(self,suffix,mode):
        path=self.path(suffix); self.paths.append(path)
        kwargs={} if 'b' in mode else {'encoding':'utf-8','newline':''}
        return self.stack.enter_context(path.open(mode,**kwargs))

    def transfer(self,raw):
        if self.xfers: self.xfers.write(struct.pack('<I',len(raw))+raw)
        if self.scope_xfers and raw[1] in (0x85,0x86):
            self.scope_xfers.write(struct.pack('<I',len(raw))+raw)

    def scope(self,samples,transfer_index,origin_ticks):
        if self.origin_ticks is None: self.origin_ticks=origin_ticks
        for i,sample in enumerate(samples):
            self.scope_count+=1
            if self.scope_writer: self.scope_writer.writerow(sample.csv_row(transfer_index,i,origin_ticks))
            if self.args.scope and self.args.ccgx3:
                # Utility graph stores CY4500 ADC counts, not ET240 registers.
                self.utility.write_scope(SimpleNamespace(
                    timestamp_us=max(0,round(ticks_to_us(sample.timestamp_ticks-origin_ticks))),
                    vbus_raw=max(0,min(65535,round(sample.vbus_mV*4096/(4048*12)))),
                    cc1_raw=max(0,min(65535,round(sample.cc1_mV*4096/4048))),
                    cc2_raw=max(0,min(65535,round(sample.cc2_mV*4096/4048))),
                    ibus_raw=max(0,min(65535,round(2048+sample.ibus_mA*36/100)))))

    def packet(self,p,origin_ticks):
        self.count+=1; self.counts[p.message]+=1
        if p.error: self.errors[p.error]+=1
        row=p.csv_row(self.count,self.previous_end,origin_ticks)
        self.previous_end=p.end_ticks
        raw=normalized_record(p,self.count,origin_ticks)
        self.records.write(raw); self.hex.write(f'{self.count-1:04d}  {raw.hex(" ")}\n')
        self.utility.rows.count+=1
        self.utility.csv.writerow(row)
        if self.utility.packets is not None: self.utility.packets.write(_packet(raw,list(map(str,row))))
        decoded=decode_capture_record(raw)
        item=p.json_row(self.count,origin_ticks)
        item['raw']=raw.hex(' '); item['raw_format']='derived CY4500 interchange, not ET240 USB'
        item['decoded']=jsonable(decoded)
        detail=''
        if p.ok and p.message=='SOURCE_CAPABILITIES':
            pdos=[decode_pdo(int.from_bytes(p.payload[i:i+4],'little'),position=(i-2)//4+1)
                  for i in range(2,len(p.payload),4)]
            self.source_pdos[p.sop]=pdos
            detail=' | '.join(format_pdo(v) for v in pdos)
        elif p.ok and p.message=='REQUEST' and len(p.payload)>=6:
            value=int.from_bytes(p.payload[2:6],'little'); pos=(value>>28)&15
            pdos=self.source_pdos.get(p.sop,[])
            detail=format_rdo(decode_rdo(value,selected_pdo=pdos[pos-1] if 1<=pos<=len(pdos) else None))
        item['detail']=detail
        self.jsonl.write(json.dumps(item,ensure_ascii=False)+'\n')
        if not self.args.quiet and not (self.args.hide_goodcrc and p.ok and p.message=='GOODCRC'):
            vbus='-' if p.vbus_mV is None else f'{p.vbus_mV/1000:.4f}V'
            print(f'REC #{self.count:04d} CC{p.cc} {p.sop:14} {p.message:28} '
                  f'ID={(p.header>>9)&7} NDO={(p.header>>12)&7} {row[1]} VBUS={vbus}'
                  + (f' {p.error}' if p.error else ''))
            if detail: print('  '+detail)

    def close(self): self.stack.close()

    def finish(self,metadata):
        self.close()
        metadata.update({'records':self.count,'scope_samples':self.scope_count,
                         'message_counts':dict(self.counts),'decode_errors':dict(self.errors),
                         'records_format':'Derived CY4500 64-byte interchange; native ET240 USB is .xfers.bin',
                         'csv_time_unit':'microseconds relative to first synchronized ET240 telemetry',
                         'csv_vbus_unit':'millivolts (CY4500 Utility convention despite Vbus(V) label)',
                         'raw_clock_hz':192000000,'scope_current':'absolute magnitude',
                         'ccgx3_encoding':'CY4500 ADC units; voltage/current quantized for Utility display',
                         'outputs':[str(p.resolve()) for p in self.paths]})
        self.path('.metadata.json').write_text(json.dumps(metadata,indent=2,ensure_ascii=False)+'\n',encoding='utf-8')
        lines=['ET240 capture summary',f"Elapsed: {metadata.get('elapsed_seconds',0):.3f} s",
               f"Interrupted Ctrl+C: {metadata.get('interrupted',False)}",f'Records: {self.count}',
               f'Scope samples: {self.scope_count}',f"USB transfers: {metadata.get('usb_transfers',0)}",
               f"Buffer overflow markers: {metadata.get('buffer_overflow_markers',0)}",
               f"Preserved telemetry trailing bytes: {metadata.get('telemetry_residual_bytes',0)}",
               f'Decode errors: {sum(self.errors.values())}']
        if metadata.get('failure'): lines.append('Failure: '+metadata['failure'])
        lines+=['','Message counts:']+[f'  {k}: {v}' for k,v in self.counts.items()]
        lines+=['','Native USB: .xfers.bin; .records.bin is derived CY4500 interchange.',
                'PD and scope use the same ET240 device clock; timestamps are session-relative.']
        self.path('.summary.txt').write_text('\n'.join(lines)+'\n',encoding='utf-8')
        print('\n'+'\n'.join(lines[:9]))
        for path in self.paths+[self.path('.summary.txt'),self.path('.metadata.json')]: print(path.resolve())


def cmd_usb_info(args):
    rows=enumerate_devices()
    if args.json: print(json.dumps(rows,indent=2,ensure_ascii=False))
    else:
        for row in rows:
            print(f"ET240 FOUND  VID:PID=1E4E:720E  Serial={row['serial']}")
            print(f"WinUSB: {row['path']}\nHID: {row['hid_path']}")
        if not rows: print('ET240 1E4E:720E not found')
    return 0 if rows else 1


def cmd_version(args):
    with ET240(device=args.device) as d: info=d.get_info()
    if args.json: print(json.dumps(info,indent=2))
    else: print(f"ET240 {info['edition_name']} firmware: {info['firmware']}\nSerial: {info['serial']}")
    return 0


def cmd_capture(args):
    if args.analyze_transitions and not args.scope: raise ValueError('--analyze-transitions requires --scope')
    if args.scope_raw and not args.scope: raise ValueError('--scope-raw requires --scope')
    if args.out_prefix is None:
        args.out_prefix=str(Path('captures')/('et240_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')))
    # Check output collisions before acquiring or configuring the hardware.
    analysis._check_output_paths([Path(args.out_prefix).with_suffix(s) for s in OUTPUT_SUFFIXES],force=args.force)
    decoder=StreamDecoder(); output=None; transfer_count=0; framing_errors=0
    metadata={'backend':'ET240 direct HID + WinUSB','interrupted':False}
    error=None; start=time.monotonic(); deadline=None
    try:
        with ET240(device=args.device,timeout_ms=250) as d:
            metadata['device']=d.get_info()
            if metadata['device']['edition']!=5:
                raise ET240Error(f"Unsupported ET240 telemetry format: hardware edition {metadata['device']['edition']}; "
                                 'this decoder supports hardware edition 5.')
            output=SessionOutput(args)
            d.start_capture(sample_rate=args.sample_rate)
            metadata['endpoints']=d.endpoints
            start=time.monotonic(); deadline=None if args.seconds is None else start+args.seconds
            next_status=start+args.status_interval
            if not args.quiet: print('ET240 capture started. Ctrl+C stops and saves the session.')
            try:
                while deadline is None or time.monotonic()<deadline:
                    raw=d.read()
                    if raw:
                        transfer_count+=1; output.transfer(raw)
                        try: packets,samples=decoder.feed(raw)
                        except FramingError:
                            framing_errors+=1
                            if not args.allow_framing_errors: raise
                            packets,samples=[],[]
                        if decoder.origin_ticks is not None:
                            output.scope(samples,transfer_count,decoder.origin_ticks)
                            for p in packets: output.packet(p,decoder.origin_ticks)
                    if not args.quiet and time.monotonic()>=next_status:
                        print(f'STATUS {time.monotonic()-start:.1f}s records={output.count} '
                              f'scope={output.scope_count} overflow={decoder.overflow_count}')
                        next_status=time.monotonic()+args.status_interval
            except KeyboardInterrupt: metadata['interrupted']=True
            finally: d.stop_capture()
    except KeyboardInterrupt: metadata['interrupted']=True
    except BaseException as exc:
        error=exc; metadata['failure']=str(exc)
    finally:
        if output is not None:
            try:
                for p in decoder.finish(): output.packet(p,decoder.origin_ticks or 0)
            finally:
                metadata.update({'elapsed_seconds':time.monotonic()-start,'usb_transfers':transfer_count,
                                 'frame_counts':dict(decoder.counts),'framing_errors':framing_errors,
                                 'buffer_overflow_markers':decoder.overflow_count,
                                 'telemetry_residual_bytes':decoder.residual_bytes,
                                 'origin_ticks':decoder.origin_ticks,'synchronized':decoder.synced})
                output.finish(metadata)
    if error is not None: raise error
    if not decoder.synced: raise ET240Error('No synchronized telemetry received; increase --seconds or check the device.')
    if args.analyze_transitions:
        args.pd_csv=str(output.path('.csv')); args.scope_csv=str(output.path('.scope.csv'))
        analysis._cmd_analyze_sync(args)
    return 0


def cmd_live_status(args):
    decoder=StreamDecoder(); count=0; window=deque(maxlen=args.median)
    with ExitStack() as stack:
        writer=None
        if args.csv:
            path=Path(args.csv)
            if path.exists() and not args.force: raise ValueError('Output exists; use --force to overwrite')
            path.parent.mkdir(parents=True,exist_ok=True)
            writer=csv.writer(stack.enter_context(path.open('w',encoding='utf-8',newline='')))
            writer.writerow(['index','timestamp_us','vbus_V','ibus_A','ibus_median_A','cc1_V','cc2_V','power_W'])
        d=stack.enter_context(ET240(device=args.device,timeout_ms=250))
        info=d.get_info()
        if info['edition']!=5:
            raise ET240Error(f"Unsupported ET240 telemetry format: hardware edition {info['edition']}; "
                             'this decoder supports hardware edition 5.')
        d.start_capture(sample_rate=args.sample_rate,bmc=False)
        next_output=0
        try:
            while args.count is None or count<args.count:
                raw=d.read()
                if not raw: continue
                _,samples=decoder.feed(raw)
                for s in samples:
                    window.append(s.ibus_A)
                    now=time.monotonic()
                    if now<next_output: continue
                    count+=1
                    row=[count,f'{ticks_to_us(s.timestamp_ticks-decoder.origin_ticks):.6f}',
                         s.vbus_V,s.ibus_A,statistics.median(window),s.cc1_mV/1000,s.cc2_mV/1000,s.power_W]
                    print(f'{count:06d} VBUS={s.vbus_V:.4f}V IBUS={row[4]:.4f}A '
                          f'CC1={row[5]:.4f}V CC2={row[6]:.4f}V P={s.power_W:.4f}W')
                    if writer: writer.writerow(row)
                    next_output=now+args.interval
                    if args.count is not None and count>=args.count: break
        except KeyboardInterrupt: pass
    return 0


def cmd_scope(args):
    # Reuse the capture path so PD edges continue to be drained and persisted.
    csv_path=Path(args.csv)
    target=csv_path.resolve()
    prefix=csv_path.with_name(csv_path.stem+'_session')
    desired=[target]+([Path(args.raw).resolve()] if args.raw else [])
    analysis._check_output_paths(desired,force=args.force)
    if len(set(desired))!=len(desired): raise ValueError('--csv and --raw must be different files')
    if set(desired)&{prefix.with_suffix(s).resolve() for s in OUTPUT_SUFFIXES}:
        raise ValueError('Scope output overlaps a companion session file; choose another --csv/--raw')
    args.out_prefix=str(prefix); args.scope=True; args.scope_raw=bool(args.raw)
    args.scope_csv_path=str(target);args.scope_raw_path=args.raw
    args.ccgx3=False; args.save_transfers=True; args.hide_goodcrc=True
    args.analyze_transitions=False; args.allow_framing_errors=False; args.status_interval=1.0
    if args.until_ctrl_c: args.seconds=None
    rc=cmd_capture(args)
    print(f'Scope CSV: {target}')
    return rc


def cmd_export_gui(args):
    source=Path(args.xfers or args.trace or args.records).resolve()
    if source in [Path(args.out_prefix).with_suffix(s).resolve() for s in OUTPUT_SUFFIXES]:
        raise ValueError('Input file overlaps an output; choose a different --out-prefix')
    args.scope=bool(args.xfers); args.scope_raw=False; args.save_transfers=False; args.ccgx3=True
    args.quiet=getattr(args,'quiet',False); args.hide_goodcrc=True
    output=SessionOutput(args); decoder=StreamDecoder(); transfers=0; error=None
    try:
        if args.xfers:
            for raw in read_transfers(args.xfers):
                transfers+=1; packets,samples=decoder.feed(raw)
                if decoder.origin_ticks is not None:
                    output.scope(samples,transfers,decoder.origin_ticks)
                    for p in packets: output.packet(p,decoder.origin_ticks)
            for p in decoder.finish(): output.packet(p,decoder.origin_ticks or 0)
        elif args.trace:
            # .gz XML contains captured BMC intervals; no vendor runtime is needed.
            with gzip.open(args.trace,'rb') as fp:
                for _,element in XML.iterparse(fp,events=('end',)):
                    if element.tag!='PdEvent': continue
                    msg=element.find('msg')
                    if msg is not None and msg.findtext('strBmc'):
                        p=decode_bmc(msg.findtext('strBmc'),0,round(float(msg.findtext('nDuration','0'))*192),
                                     int(msg.findtext('cc','0'))+1)
                        dt=element.findtext('dateTime','0001-01-01T00:00:00')
                        clock=datetime.fromisoformat(dt)
                        seconds=(clock-datetime(1,1,1)).total_seconds()
                        p.start_ticks=round(seconds*192000000); p.end_ticks+=p.start_ticks
                        if decoder.origin_ticks is None: decoder.origin_ticks=p.start_ticks
                        output.packet(p,decoder.origin_ticks)
                    element.clear()
        elif args.records:
            previous=None
            with source.open('rb') as fp:
                while True:
                    raw=fp.read(64)
                    if not raw: break
                    if len(raw)!=64: raise FramingError('Truncated derived 64-byte record')
                    d=decode_capture_record(raw); f=d['fields']
                    start=d['start_time']
                    if previous is not None:
                        start=previous+(start-previous+(1<<31))%(1<<32)-(1<<31)
                    previous=start
                    end=start+((d['end_time']-d['start_time'])&0xFFFFFFFF)
                    sop=d['sop_name']
                    payload=(d['pd_header'].to_bytes(2,'little')+raw[20:20+4*f['OBJ_COUNT']]
                             if sop not in ('HARD_RESET','CABLE_RESET') else b'')
                    p=PDPacket(start*192,end*192,0,sop,payload,crc_ok=bool(f['OK']),
                               error='' if f['OK'] else ('CRC mismatch' if f['CRC_ERROR'] else 'Derived record error'),
                               vbus_mV=d['vbus_V']*1000)
                    if decoder.origin_ticks is None: decoder.origin_ticks=0
                    output.packet(p,0)
    except BaseException as exc: error=exc
    finally:
        output.finish({'source':str(source),'usb_transfers':transfers,
                       'origin_ticks':decoder.origin_ticks,'telemetry_residual_bytes':decoder.residual_bytes,
                       'buffer_overflow_markers':decoder.overflow_count,
                       **({'failure':str(error)} if error else {})})
    if error: raise error
    return 0


def cmd_unsupported(args):
    raise ET240Error(f'{args.command}: no verified ET240 equivalent is available. '
                     'CY4500 hardware trigger/CC termination commands cannot be sent to ET240.')


def device_option(p):
    p.add_argument('--device',help='ET240 USB serial; required if multiple devices are connected')


def build_arg_parser():
    parser=argparse.ArgumentParser(description='Direct ET240 HID + WinUSB capture, with CY4500 command/CSV compatibility')
    sub=parser.add_subparsers(dest='command',required=True)
    p=sub.add_parser('usb-info',help='enumerate ET240 USB/HID interfaces without opening the device')
    p.add_argument('--json',action='store_true'); p.set_defaults(func=cmd_usb_info)
    p=sub.add_parser('version',help='read ET240 firmware and device identity');device_option(p)
    p.add_argument('--json',action='store_true'); p.set_defaults(func=cmd_version)
    p=sub.add_parser('capture',help='capture PD + scope until Ctrl+C by default'); device_option(p)
    duration=p.add_mutually_exclusive_group()
    duration.add_argument('--seconds',type=positive)
    duration.add_argument('--until-ctrl-c',action='store_true')
    p.add_argument('--out-prefix');p.add_argument('--force',action='store_true')
    g=p.add_mutually_exclusive_group();g.add_argument('--scope',action='store_true');g.add_argument('--no-scope',dest='scope',action='store_false')
    g=p.add_mutually_exclusive_group();g.add_argument('--ccgx3',action='store_true');g.add_argument('--no-ccgx3',dest='ccgx3',action='store_false')
    g=p.add_mutually_exclusive_group();g.add_argument('--hide-goodcrc',action='store_true');g.add_argument('--show-goodcrc',dest='hide_goodcrc',action='store_false')
    p.add_argument('--scope-raw',action='store_true',help='save native ET240 telemetry transfers')
    p.add_argument('--quiet',action='store_true');p.add_argument('--status-interval',type=positive,default=1.0)
    p.add_argument('--allow-framing-errors',action='store_true');p.add_argument('--gui-csv',action='store_true',help='compatibility alias; Utility CSV is always written')
    p.add_argument('--sample-rate',type=int,choices=range(5),default=3,help='ET240 firmware sample-rate index 0..4 (default: 3)')
    p.add_argument('--analyze-transitions',action='store_true'); analysis._add_transition_analysis_options(p)
    p.set_defaults(func=cmd_capture,scope=True,ccgx3=True,hide_goodcrc=True,save_transfers=True)
    p=sub.add_parser('live-status',aliases=['volt-amp'],help='read live telemetry; consumes an ET240 acquisition session');device_option(p)
    p.add_argument('--count',type=positive_int);p.add_argument('--interval',type=positive,default=0.1)
    p.add_argument('--median',type=positive_int,default=5);p.add_argument('--csv');p.add_argument('--force',action='store_true')
    p.add_argument('--sample-rate',type=int,choices=range(5),default=3);p.set_defaults(func=cmd_live_status)
    p=sub.add_parser('scope',help='capture scope CSV while also draining/saving PD');device_option(p)
    g=p.add_mutually_exclusive_group();g.add_argument('--seconds',type=positive,default=5.0);g.add_argument('--until-ctrl-c',action='store_true')
    p.add_argument('--csv',default='et240_scope.csv');p.add_argument('--raw');p.add_argument('--quiet',action='store_true');p.add_argument('--force',action='store_true')
    p.add_argument('--sample-rate',type=int,choices=range(5),default=3);p.set_defaults(func=cmd_scope)
    p=sub.add_parser('export-gui',help='offline native ET240 transfer / Sniffer trace conversion')
    g=p.add_mutually_exclusive_group(required=True);g.add_argument('--xfers',help='native ET240 .xfers.bin');g.add_argument('--trace',help='Sniffer .gz XML trace (PD only)')
    g.add_argument('--records',help='derived CY4500-compatible .records.bin (PD only)')
    p.add_argument('--out-prefix',required=True);p.add_argument('--force',action='store_true');p.add_argument('--quiet',action='store_true');p.set_defaults(func=cmd_export_gui)
    p=sub.add_parser('analyze-sync',help='offline PPS/AVS transition analysis; same options as CY4500')
    p.add_argument('--pd-csv',required=True);p.add_argument('--scope-csv',required=True)
    p.add_argument('--out-prefix',default='et240_sync_analysis');p.add_argument('--force',action='store_true')
    analysis._add_transition_analysis_options(p);p.set_defaults(func=analysis._cmd_analyze_sync)
    for name in ('trigger','trigger-arm','trigger-epr-request','trigger-clear','termination','termination-clear'):
        p=sub.add_parser(name,help='unsupported: no verified ET240 hardware equivalent')
        p.add_argument('arguments',nargs=argparse.REMAINDER);p.set_defaults(func=cmd_unsupported)
    return parser


def main(argv=None):
    parser=build_arg_parser();args=parser.parse_args(argv)
    try: return int(args.func(args))
    except (ET240Error,ValueError,OSError,XML.ParseError) as exc:
        print(f'ERROR: {exc}',file=sys.stderr);return 2
    except KeyboardInterrupt: return 130


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'): sys.stdout.reconfigure(errors='replace')
    raise SystemExit(main())
