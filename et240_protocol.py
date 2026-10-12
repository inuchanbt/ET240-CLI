"""Pure ET240 framing, telemetry and CC BMC decoding.

Wire layout verified against Sniffer 1.08.26.0809 and a connected SN3005.
Device counters run at 192 MHz; the low counter wraps at 2**24.
"""
from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, asdict
import struct
import zlib

from ezpd_protocol import get_message_name, SPEC_REV, decode_pdo, decode_rdo, format_pdo, format_rdo

NIBBLES = dict(zip(('01111','10010','00101','10101','01010','11010','01110','11110',
                    '01001','11001','01101','11101','01011','11011','00111','10111'), range(16)))
SYNC1, SYNC2, SYNC3, RST1, RST2, EOP = '00011','10001','01100','11100','10011','10110'
ORDERED_SETS = {
    (SYNC1,SYNC1,SYNC1,SYNC2): 'SOP',
    (SYNC1,SYNC1,SYNC3,SYNC3): 'SOP_PRIME',
    (SYNC1,SYNC3,SYNC1,SYNC3): 'SOP_DPRIME',
    (SYNC1,RST2,RST2,SYNC3): 'SOP_P_DBG',
    (SYNC1,RST2,SYNC3,SYNC2): 'SOP_DP_DBG',
    (RST1,RST1,RST1,RST2): 'HARD_RESET',
    (RST1,SYNC1,RST1,SYNC3): 'CABLE_RESET',
}
CSV_COLUMNS = 'Sno,Ok,SOP,Message,Msg Id,Data Role,Power Role,Obj Count,Rev,Duration,Delta,Vbus(V),Data,Start Time,End Time'.split(',')
SCOPE_COLUMNS = ['transfer_index','packet_index','sample_index','timestamp_raw','timestamp_us',
                 'vbus_raw','vbus_mV','vbus_V','cc1_raw','cc1_mV','cc1_V','cc2_raw','cc2_mV',
                 'cc2_V','ibus_raw','ibus_mA','ibus_A','power_W','padding_hex',
                 'device_epoch','device_counter','cc1_current_mA','cc2_current_mA','gpio']


class FramingError(ValueError):
    pass


def frame_payload(raw):
    if len(raw) < 4 or raw[0] != 0xEE:
        raise FramingError('Expected ET240 EE/type/uint16-length header')
    length = struct.unpack_from('<H', raw, 2)[0]
    if length != len(raw) - 4:
        raise FramingError(f'ET240 declared {length} bytes, received {len(raw)-4}')
    if raw[1] not in (0x81,0x82,0x83,0x85,0x86):
        raise FramingError(f'Unknown ET240 frame type 0x{raw[1]:02X}')
    return raw[1], raw[4:]


def ticks_to_us(ticks):
    return ticks / 192


def signed20(value):
    value >>= 4
    return value - (1 << 20) if value & (1 << 19) else value


@dataclass
class ScopeSample:
    timestamp_ticks: int
    device_epoch: int
    device_counter: int
    gpio: int
    adc_raw: tuple
    vbus_raw: int
    ibus_raw: int
    vbus_mV: float
    ibus_mA: float
    cc1_mV: float
    cc2_mV: float
    power_W: float
    cc1_current_mA: float
    cc2_current_mA: float

    @property
    def timestamp_us(self):
        return ticks_to_us(self.timestamp_ticks)

    @property
    def vbus_V(self):
        return self.vbus_mV / 1000

    @property
    def ibus_A(self):
        return self.ibus_mA / 1000

    def csv_row(self, transfer_index, packet_index, origin_ticks=0):
        return [transfer_index,packet_index,packet_index,self.device_counter,
                f'{ticks_to_us(self.timestamp_ticks-origin_ticks):.6f}',self.vbus_raw,
                self.vbus_mV,self.vbus_V,self.adc_raw[0],self.cc1_mV,self.cc1_mV/1000,
                self.adc_raw[1],self.cc2_mV,self.cc2_mV/1000,self.ibus_raw,self.ibus_mA,
                self.ibus_A,self.power_W,'',self.device_epoch,self.device_counter,
                self.cc1_current_mA,self.cc2_current_mA,self.gpio]


def decode_scope_block(block):
    if len(block) != 64:
        raise FramingError('ET240 telemetry block must be 64 bytes')
    epoch, counter_gpio = struct.unpack_from('<II',block)
    adc = struct.unpack_from('<8H',block,8)
    vbus, power, ibus = struct.unpack_from('<III',block,24)
    cc1_v, cc1_p, cc1_i, cc2_v, cc2_p, cc2_i = struct.unpack_from('<6I',block,36)
    counter = counter_gpio & 0xFFFFFF
    # ADC_RATIO / board resistor-divider ratio, from PdUtil.Initial.
    cc_scale_mV = 0.0008058608 / 0.5454 * 1000
    return ScopeSample((epoch << 24) + counter, epoch, counter, counter_gpio >> 24,
                       adc,vbus,ibus,signed20(vbus)*25/128,abs(signed20(ibus))/32,
                       adc[0]*cc_scale_mV,adc[1]*cc_scale_mV,power*0.1/1000,
                       abs(signed20(cc1_i))/64,abs(signed20(cc2_i))/64)


@dataclass
class PDPacket:
    start_ticks: int
    end_ticks: int
    cc: int
    sop: str = ''
    payload: bytes = b''  # PD header + data, excluding CRC
    crc: bytes = b''
    crc_ok: bool = False
    error: str = ''
    intervals: str = ''
    vbus_mV: float | None = None

    @property
    def header(self):
        return int.from_bytes(self.payload[:2],'little') if len(self.payload)>=2 else 0

    @property
    def message_class(self):
        return 'EXTENDED' if self.header & 0x8000 else ('DATA' if self.header & 0x7000 else 'CONTROL')

    @property
    def message(self):
        if self.sop in ('HARD_RESET','CABLE_RESET'):
            return self.sop
        if not self.payload:
            return 'DECODE_ERROR'
        return get_message_name(self.message_class,self.header & 31,'UNKNOWN')

    @property
    def ok(self):
        return not self.error and (self.crc_ok or self.sop in ('HARD_RESET','CABLE_RESET'))

    def json_row(self,index,origin_ticks):
        return {'index':index,'cc':self.cc,'sop':self.sop,'message':self.message,
                'message_class':self.message_class,'ok':self.ok,'crc_ok':self.crc_ok,
                'error':self.error,'header':self.header,'payload_hex':self.payload.hex(' '),
                'crc_hex':self.crc.hex(' '),'start_ticks':self.start_ticks,'end_ticks':self.end_ticks,
                'start_us':ticks_to_us(self.start_ticks-origin_ticks),
                'end_us':ticks_to_us(self.end_ticks-origin_ticks),'vbus_mV':self.vbus_mV,
                'bmc_intervals':self.intervals}

    def csv_row(self,index,previous_end,origin_ticks):
        h=self.header; count=(h>>12)&7
        data=''
        if self.payload:
            data=f'0x{h:X}'
            if h&0x8000:
                data+=f" 0x{int.from_bytes(self.payload[2:4],'little'):X}"
                data+=''.join(f' 0x{b:02X}' for b in self.payload[4:])
            else:
                data+=''.join(f" 0x{int.from_bytes(self.payload[i:i+4],'little'):X}" for i in range(2,len(self.payload),4))
        status='OK' if self.ok else ('ER_CRC' if self.error=='CRC mismatch' else 'ER_DECODE')
        header_present=bool(self.payload)
        return [index,status,self.sop,self.message,(h>>9)&7 if header_present else '',
                ('DFP' if h&32 else 'UFP') if self.sop=='SOP' and header_present else '',
                ('SRC' if h&256 else 'SNK') if self.sop=='SOP' and header_present else '',
                count if header_present else '',SPEC_REV.get((h>>6)&3,'UNKNOWN') if header_present else '',
                f'{ticks_to_us(self.end_ticks-self.start_ticks):.6f}',
                '' if previous_end is None else f'{ticks_to_us(self.start_ticks-previous_end):.6f}',
                '' if self.vbus_mV is None else round(self.vbus_mV),data,
                f'{ticks_to_us(self.start_ticks-origin_ticks):.6f}',
                f'{ticks_to_us(self.end_ticks-origin_ticks):.6f}']


def decode_bmc(intervals,start_ticks,end_ticks,cc):
    packet=PDPacket(start_ticks,end_ticks,cc,intervals=intervals)
    start=intervals.find('011011011011')
    if start<0:
        packet.error='Invalid preamble'; return packet
    bits=[]; i=start
    while i<len(intervals):
        if intervals[i]=='0': bits.append('0'); i+=1
        elif intervals[i:i+2]=='11': bits.append('1'); i+=2
        elif i==len(intervals)-1: break  # trailing half-bit after EOP
        else:
            packet.error='Invalid BMC half-bit pair'; return packet
    bits=''.join(bits)
    pos=0
    while bits[pos:pos+2]=='01': pos+=2
    if pos+20>len(bits):
        packet.error='Missing ordered set'; return packet
    ordered=tuple(bits[j:j+5] for j in range(pos,pos+20,5))
    packet.sop=ORDERED_SETS.get(ordered,'')
    if not packet.sop:
        packet.error='Unknown ordered set'; return packet
    if packet.sop in ('HARD_RESET','CABLE_RESET'):
        return packet
    pos+=20; nibbles=[]; eop=False
    while pos+5<=len(bits):
        symbol=bits[pos:pos+5]; pos+=5
        if symbol==EOP:
            eop=True; break
        if symbol not in NIBBLES:
            packet.error='Invalid 4b5b symbol'; return packet
        nibbles.append(NIBBLES[symbol])
    if len(nibbles)%2 or len(nibbles)<12:
        packet.error='Invalid packet/CRC length'; return packet
    raw=bytes(nibbles[j]|(nibbles[j+1]<<4) for j in range(0,len(nibbles),2))
    packet.payload,packet.crc=raw[:-4],raw[-4:]
    packet.crc_ok=zlib.crc32(packet.payload)==int.from_bytes(packet.crc,'little')
    expected=2+4*((packet.header>>12)&7)
    if not eop: packet.error='Missing EOP'
    elif len(packet.payload)!=expected: packet.error='Header/object-count length mismatch'
    elif not packet.crc_ok: packet.error='CRC mismatch'
    return packet


class BMCChannel:
    def __init__(self,cc):
        self.cc=cc; self.previous_level=None; self.last_edge=None
        self.start=None; self.intervals=[]

    def flush(self):
        result=[]
        if len(self.intervals)>110:
            result.append(decode_bmc(''.join(self.intervals),self.start,self.last_edge,self.cc))
        self.intervals=[]; self.start=None
        return result

    def feed(self,ticks,level):
        out=[]
        if self.last_edge is not None and ticks-self.last_edge>=1152:
            out+=self.flush()
        if self.previous_level is None or level==self.previous_level:
            self.previous_level=level
            return out
        if self.last_edge is not None:
            delta=ticks-self.last_edge
            if 173<=delta<500: symbol='1'
            elif 500<=delta<1152: symbol='0'
            else:
                out+=self.flush(); symbol=None
            if symbol is not None:
                if self.start is None: self.start=self.last_edge
                self.intervals.append(symbol)
                if len(self.intervals)>20000:
                    out+=self.flush()  # bounded memory for BIST/noisy captures
        self.previous_level=level; self.last_edge=ticks
        return out


class StreamDecoder:
    def __init__(self):
        self.channels=[BMCChannel(1),BMCChannel(2)]
        self.epoch=0; self.last_counter=0; self.origin_ticks=None
        self.synced=False; self.warmup_immediate=0
        self.scope_history=deque(maxlen=2000)
        self.counts=Counter(); self.overflow_count=0; self.residual_bytes=0

    def _associate(self,packets):
        for p in packets:
            # Device-timestamped, most recent telemetry preceding the PD packet.
            sample=next((s for s in reversed(self.scope_history) if s.timestamp_ticks<=p.start_ticks),None)
            if sample is not None and p.start_ticks-sample.timestamp_ticks<=192_000_000:
                p.vbus_mV=sample.vbus_mV
        return packets

    def feed(self,raw):
        kind,payload=frame_payload(raw); self.counts[f'0x{kind:02X}']+=1
        packets=[]; samples=[]
        if not self.synced and kind!=0x85: self.warmup_immediate=0
        if kind in (0x85,0x86):
            # Firmware caps a full USB transfer at 508 payload bytes. Match the
            # official decoder: preserve, count, but do not invent partial samples.
            self.residual_bytes+=len(payload)%64
            if kind==0x85 and len(payload)!=64:
                raise FramingError('Immediate telemetry must contain one 64-byte sample')
            for offset in range(0,len(payload)-63,64):
                sample=decode_scope_block(payload[offset:offset+64])
                if kind==0x85:
                    self.warmup_immediate+=1
                    if not self.synced and self.warmup_immediate<4: continue
                    self.synced=True
                    self.epoch=sample.device_epoch; self.last_counter=sample.device_counter
                if not self.synced: continue
                if self.origin_ticks is None: self.origin_ticks=sample.timestamp_ticks
                self.scope_history.append(sample); samples.append(sample)
            if samples:
                watermark=samples[-1].timestamp_ticks
                for ch in self.channels:
                    if ch.last_edge is not None and watermark-ch.last_edge>=1152:
                        packets+=ch.flush()
        elif kind==0x81:
            if len(payload)%4: raise FramingError('BMC payload must contain uint32 edge records')
            if not self.synced: return [],[]
            for (word,) in struct.iter_unpack('<I',payload):
                counter=word&0xFFFFFF; levels=word>>24
                if counter<self.last_counter: self.epoch+=1
                self.last_counter=counter
                ticks=(self.epoch<<24)+counter
                if levels&0x80: self.overflow_count+=1
                for bit,ch in enumerate(self.channels):
                    packets+=ch.feed(ticks,(levels>>bit)&1)
        return self._associate(sorted(packets,key=lambda p:p.start_ticks)),samples

    def finish(self):
        return self._associate(sorted([p for ch in self.channels for p in ch.flush()],key=lambda p:p.start_ticks))


def read_transfers(path):
    with open(path,'rb') as fp:
        while True:
            length=fp.read(4)
            if not length: return
            if len(length)!=4: raise FramingError('Truncated transfer length')
            size=struct.unpack('<I',length)[0]
            if not 4<=size<=512: raise FramingError(f'Invalid ET240 transfer size {size}')
            raw=fp.read(size)
            if len(raw)!=size: raise FramingError('Truncated ET240 transfer')
            yield raw
