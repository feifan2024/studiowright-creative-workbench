"""Advertise only how to locate StudioWright's existing SSH host on a LAN.

The advertisement contains no credentials and does not expose a web service.
The Windows client must still authenticate and verify the SSH host key.
"""
from __future__ import annotations

import argparse
import getpass
import ipaddress
import json
import os
import re
import socket
import time

PORT=18925
MAGIC=b'STUDIOWRIGHT_DISCOVER_V1\n'
NETWORKS=tuple(ipaddress.ip_network(s) for s in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16','169.254.0.0/16','127.0.0.0/8'))

def allowed_address(value):
    try:
        addr=ipaddress.ip_address(value)
        return addr.version==4 and any(addr in network for network in NETWORKS)
    except ValueError:
        return False

def make_offer(ssh_port=22):
    host=socket.gethostname()
    user=getpass.getuser()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',host):raise ValueError('Invalid host name')
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_-]{0,63}',user):raise ValueError('Invalid SSH user')
    if not 1<=ssh_port<=65535:raise ValueError('Invalid SSH port')
    return json.dumps({'product':'StudioWright','protocol':1,'host':host,'user':user,
                       'ssh_port':ssh_port,'workbench_port':18920},separators=(',',':')).encode('ascii')

def serve(port=PORT,broadcast=True,ssh_port=22):
    offer=make_offer(ssh_port)
    last_reply={}
    next_announcement=0.0
    with socket.socket(socket.AF_INET,socket.SOCK_DGRAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET,socket.SO_BROADCAST,1)
        sock.bind(('',port))
        sock.settimeout(1.0)
        print('discovery service ready',flush=True)
        while True:
            now=time.monotonic()
            if broadcast and now>=next_announcement:
                try:sock.sendto(offer,('255.255.255.255',port))
                except OSError:pass
                next_announcement=now+30
            try:message,peer=sock.recvfrom(512)
            except socket.timeout:continue
            if message!=MAGIC or not allowed_address(peer[0]):continue
            now=time.monotonic()
            if now-last_reply.get(peer[0],-100)<1:continue
            last_reply[peer[0]]=now
            if len(last_reply)>256:
                last_reply={key:value for key,value in last_reply.items() if now-value<60}
            try:sock.sendto(offer,peer)
            except OSError:pass

def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--port',type=int,default=PORT)
    parser.add_argument('--ssh-port',type=int,default=int(os.environ.get('STUDIOWRIGHT_DISCOVERY_SSH_PORT','22')))
    parser.add_argument('--no-broadcast',action='store_true')
    args=parser.parse_args()
    if not 1<=args.port<=65535:parser.error('invalid discovery port')
    serve(args.port,not args.no_broadcast,args.ssh_port)

if __name__=='__main__':
    main()
