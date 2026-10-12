import base64
from collections import Counter
import csv
import gzip
import importlib
import contextlib
import io
import json
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import xml.etree.ElementTree as XML
import zlib

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import et240_cli as cli
from et240_protocol import *
from et240_usb import build_config, ET240Error


def frame(kind,payload):
    return bytes((0xEE,kind))+struct.pack('<H',len(payload))+payload


def telemetry(ticks=0,vbus=5000,current=1000):
    b=bytearray(64)
    struct.pack_into('<II',b,0,ticks>>24,ticks&0xFFFFFF)
    struct.pack_into('<8H',b,8,500,600,100,200,0,0,0,0)
    struct.pack_into('<III',b,24,round(vbus*128/25)<<4,50000,round(current*32)<<4)
    b[60:64]=bytes.fromhex('e3e2e1e0')
    return bytes(b)


def bmc(payload=b'\x81\x01',bad_crc=False,sop=(SYNC1,SYNC1,SYNC1,SYNC2)):
    crc=zlib.crc32(payload)^(1 if bad_crc else 0)
    raw=payload+crc.to_bytes(4,'little')
    encoding={v:k for k,v in NIBBLES.items()}
    bits='01'*32+''.join(sop)+''.join(encoding[b&15]+encoding[b>>4] for b in raw)+EOP
    return ''.join('0' if b=='0' else '11' for b in bits)


def synthetic_transfers(intervals=None,split=37):
    result=[frame(0x85,telemetry())]*4
    ticks=5000; level=0
    words=[ticks]
    for s in intervals or bmc():
        ticks+=320 if s=='1' else 640
        level^=1
        words.append((level<<24)|ticks)
    words.append((level<<24)|(ticks+2000))
    for i in range(0,len(words),split):
        result.append(frame(0x81,struct.pack('<'+'I'*len(words[i:i+split]),*words[i:i+split])))
    result.append(frame(0x85,telemetry(ticks+4000)))
    return result


class ProtocolTests(unittest.TestCase):
    def test_config_matches_vendor(self):
        b=build_config()
        self.assertEqual(len(b),48);self.assertEqual(b[:3],b'\x03\0\0')
        self.assertEqual(b[8:11],b'\x03\0\x80')
        self.assertEqual(b[16:30],bytes.fromhex('43270200432708000000fb691000'))

    def test_decode_and_bad_crc(self):
        p=decode_bmc(bmc(),0,10,1)
        self.assertTrue(p.ok);self.assertEqual(p.message,'GOODCRC')
        self.assertEqual(p.payload,b'\x81\x01')
        self.assertEqual(decode_bmc(bmc(bad_crc=True),0,10,1).error,'CRC mismatch')

    def test_hard_reset(self):
        bits='01'*32+RST1*3+RST2
        intervals=''.join('0' if b=='0' else '11' for b in bits)
        p=decode_bmc(intervals,0,10,2)
        self.assertTrue(p.ok);self.assertEqual(p.message,'HARD_RESET')

    def test_stream_cross_transfer_boundaries(self):
        for split in (1,17,37,127):
            decoder=StreamDecoder();packets=[]
            for raw in synthetic_transfers(split=split):
                p,s=decoder.feed(raw);packets+=p
            packets+=decoder.finish()
            self.assertEqual(len(packets),1,split)
            self.assertTrue(packets[0].ok)
            self.assertAlmostEqual(packets[0].vbus_mV,5000,delta=.2)

    def test_telemetry_and_signed_current(self):
        s=decode_scope_block(telemetry(1<<24))
        self.assertEqual(s.device_epoch,1)
        self.assertAlmostEqual(s.vbus_V,5,delta=.001)
        self.assertEqual(s.ibus_A,1)
        b=bytearray(telemetry())
        struct.pack_into('<I',b,32,((1<<20)-32000)<<4)
        self.assertEqual(decode_scope_block(b).ibus_A,1)

    def test_strict_framing(self):
        for raw in (b'',b'\xee\x81\x04\0',b'\xef\x81\0\0',b'\xee\xff\0\0'):
            with self.assertRaises(FramingError): frame_payload(raw)

    def test_counter_wrap(self):
        decoder=StreamDecoder()
        for _ in range(4): decoder.feed(frame(0x85,telemetry((7<<24)+0xFFFF00)))
        decoder.feed(frame(0x81,struct.pack('<II',0xFFFFF0,0x01000040)))
        self.assertEqual(decoder.epoch,8)

    def test_vendor_examples_payload_crc_and_sop(self):
        samples=ROOT.parent/'PD_Sniffer v1.08.26.0809'/'trace_example'
        if not samples.exists(): self.skipTest('Vendor examples are not installed')
        count=0
        sop_map={'SOP_P':'SOP_PRIME','SOP_Dp':'SOP_DPRIME','SOP_P_Dbg':'SOP_P_DBG','SOP_Dp_Dbg':'SOP_DP_DBG'}
        for path in samples.glob('*.gz'):
            tree=XML.fromstring(gzip.decompress(path.read_bytes()))
            for m in tree.findall('.//msg'):
                raw=m.findtext('strBmc')
                if not raw: continue
                p=decode_bmc(raw,0,0,1)
                self.assertEqual(p.payload,base64.b64decode(m.findtext('payload','')))
                self.assertEqual(p.crc,base64.b64decode(m.findtext('crc','')))
                self.assertEqual(p.crc_ok,m.findtext('IsCrcOk')=='true')
                self.assertEqual(p.sop,sop_map.get(m.findtext('sop'),m.findtext('sop')))
                self.assertTrue(p.ok,p.error);count+=1
        self.assertEqual(count,228)


class CLITests(unittest.TestCase):
    def setUp(self):
        temp_root=ROOT/'test_output'
        temp_root.mkdir(exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=temp_root);self.addCleanup(self.temp.cleanup)
        self.stdout=contextlib.redirect_stdout(io.StringIO());self.stdout.__enter__();self.addCleanup(self.stdout.__exit__,None,None,None)
        self.stderr=contextlib.redirect_stderr(io.StringIO());self.stderr.__enter__();self.addCleanup(self.stderr.__exit__,None,None,None)
        self.root=Path(self.temp.name)

    def test_offline_replay_outputs_and_analysis(self):
        raw=self.root/'input.xfers.bin'
        raw.write_bytes(b''.join(struct.pack('<I',len(b))+b for b in synthetic_transfers()))
        prefix=self.root/'out'
        self.assertEqual(cli.main(['export-gui','--xfers',str(raw),'--out-prefix',str(prefix),'--quiet']),0)
        with prefix.with_suffix('.csv').open() as fp:
            reader=csv.DictReader(fp);rows=list(reader)
            self.assertEqual(reader.fieldnames,CSV_COLUMNS)
        self.assertEqual(rows[0]['Message'],'GOODCRC');self.assertEqual(rows[0]['Vbus(V)'],'5000')
        self.assertEqual(prefix.with_suffix('.records.bin').stat().st_size,64)
        self.assertTrue(prefix.with_suffix('.ccgx3').exists())
        self.assertEqual(cli.main(['analyze-sync','--pd-csv',str(prefix.with_suffix('.csv')),
                                  '--scope-csv',str(prefix.with_suffix('.scope.csv')),
                                  '--out-prefix',str(self.root/'analysis')]),0)
        self.assertTrue((self.root/'analysis.transitions.csv').exists())

    def test_existing_output_rejected_before_hardware(self):
        prefix=self.root/'session';prefix.with_suffix('.csv').write_text('keep')
        with patch.object(cli,'ET240') as device:
            self.assertEqual(cli.main(['capture','--out-prefix',str(prefix),'--seconds','1']),2)
            device.assert_not_called()
        self.assertEqual(prefix.with_suffix('.csv').read_text(),'keep')

    def test_nonempty_pps_analysis(self):
        prefix=self.root/'pps'
        args=cli.build_arg_parser().parse_args(['capture','--out-prefix',str(prefix),'--quiet'])
        output=cli.SessionOutput(args)
        def packet(us,h,data=b''):
            return PDPacket(us*192,(us+100)*192,1,'SOP',h.to_bytes(2,'little')+data,
                            crc_ok=True,vbus_mV=5000)
        pdo=0xC0000000|(200<<17)|(33<<8)|60
        request=(1<<28)|(500<<9)|40
        output.packet(packet(0,0x11A1,pdo.to_bytes(4,'little')),0)
        output.packet(packet(200000,0x1082,request.to_bytes(4,'little')),0)
        output.packet(packet(201000,0x01A3),0)
        output.packet(packet(230000,0x01A6),0)
        for us in range(0,500000,1000):
            voltage=5000 if us<202000 else min(10000,5000+(us-202000)*.5)
            output.scope([decode_scope_block(telemetry(us*192,voltage))],1,0)
        output.finish({})
        analysis_prefix=self.root/'pps_analysis'
        self.assertEqual(cli.main(['analyze-sync','--pd-csv',str(prefix.with_suffix('.csv')),
                                   '--scope-csv',str(prefix.with_suffix('.scope.csv')),
                                   '--out-prefix',str(analysis_prefix)]),0)
        with analysis_prefix.with_suffix('.transitions.csv').open(encoding='utf-8-sig') as fp:
            rows=list(csv.DictReader(fp))
        self.assertEqual(len(rows),1)
        self.assertEqual(float(rows[0]['target_voltage_V']),10)

    def test_normalized_record_export_and_source_protection(self):
        prefix=self.root/'original'
        args=cli.build_arg_parser().parse_args(['capture','--out-prefix',str(prefix),'--quiet'])
        output=cli.SessionOutput(args)
        output.packet(decode_bmc(bmc(),0,640,1),0);output.finish({})
        source=prefix.with_suffix('.records.bin')
        self.assertEqual(cli.main(['export-gui','--records',str(source),'--out-prefix',str(self.root/'converted'),'--quiet']),0)
        old=source.read_bytes()
        self.assertEqual(cli.main(['export-gui','--records',str(source),'--out-prefix',str(prefix),'--force','--quiet']),2)
        self.assertEqual(source.read_bytes(),old)

    def test_capture_cleanup_after_failure(self):
        class Fake:
            stopped=False;closed=False
            def __init__(self,**kwargs): self.endpoints=[]
            def __enter__(self): return self
            def __exit__(self,*args): Fake.closed=True
            def get_info(self): return {'edition':5}
            def start_capture(self,**kwargs): pass
            def read(self): raise ET240Error('Disconnected')
            def stop_capture(self): Fake.stopped=True
        with patch.object(cli,'ET240',Fake):
            self.assertEqual(cli.main(['capture','--out-prefix',str(self.root/'failed'),'--quiet']),2)
        self.assertTrue(Fake.stopped and Fake.closed)
        metadata=json.loads((self.root/'failed.metadata.json').read_text())
        self.assertEqual(metadata['failure'],'Disconnected')

    def test_default_options_and_duration_validation(self):
        args=cli.build_arg_parser().parse_args(['capture'])
        self.assertIsNone(args.seconds);self.assertTrue(args.scope and args.ccgx3 and args.hide_goodcrc)
        for value in ('0','-1','nan','inf'):
            with self.assertRaises(SystemExit): cli.build_arg_parser().parse_args(['capture','--seconds',value])

    def test_unsupported_never_opens_hardware(self):
        with patch.object(cli,'ET240') as device:
            self.assertEqual(cli.main(['termination-clear']),2);device.assert_not_called()

    def test_scope_keeps_pd_csv_separate(self):
        target=self.root/'scope.csv'
        with patch.object(cli,'cmd_capture',return_value=0) as capture:
            self.assertEqual(cli.main(['scope','--seconds','1','--csv',str(target)]),0)
        args=capture.call_args.args[0]
        self.assertNotEqual(Path(args.out_prefix).with_suffix('.csv'),target)
        self.assertEqual(Path(args.scope_csv_path),target.resolve())


if __name__=='__main__': unittest.main()
