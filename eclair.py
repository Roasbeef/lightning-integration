from binascii import hexlify
from lnaddr import lndecode
from utils import TailableProc


import json
import logging
import os
import psutil
import re
import requests
import time


class EclairD(TailableProc):
    def __init__(self, lightning_dir, bitcoin_dir, port):
        TailableProc.__init__(self, lightning_dir)
        self.lightning_dir = lightning_dir
        self.bitcoin_dir = bitcoin_dir
        self.port = port
        self.rpc_port = str(10000 + port)
        self.prefix = 'eclair'

        self.cmd_line = [
            '/usr/lib/jvm/java-8-openjdk-amd64/bin/java',
            '-jar',
            'bin/eclair.jar',
            '--datadir={}'.format(lightning_dir)
        ]

        if not os.path.exists(lightning_dir):
            os.makedirs(lightning_dir)

        # Adapt the config and store it
        config = open('configs/eclair.conf').read()
        config = config.replace('9735', str(port))
        config = config.replace('18332', str(28332))
        config = config.replace('8080', str(self.rpc_port))

        with open(os.path.join(lightning_dir, "eclair.conf"), "w") as f:
            f.write(config)

    def start(self):
        TailableProc.start(self)
        self.wait_for_log("connected to tcp://127.0.0.1:29000")
        self.wait_for_log("Successfully bound to /127.0.0.1:{}".format(self.rpc_port))

        # And let's also remember the address
        exp = 'finaladdress=([mn][a-zA-Z0-9]+)'
        addr_line = self.wait_for_log(exp)
        self.addr = re.search(exp, addr_line).group(1)

        logging.info("Eclair started (pid: {})".format(self.proc.pid))

    def stop(self):
        # Java forks internally and detaches its children, use psutil to hunt
        # them down and kill them
        proc = psutil.Process(self.proc.pid)
        processes = [proc] + proc.children(recursive=True)

        # Be nice to begin with
        for p in processes:
            p.terminate()
        _, alive = psutil.wait_procs(processes, timeout=3)

        # But if they aren't, we can be more persuasive
        for p in alive:
            p.kill()
        psutil.wait_procs(alive, timeout=3)


class EclairNode(object):

    def __init__(self, lightning_dir, lightning_port, btc, executor=None,
                 node_id=0):
        self.bitcoin = btc
        self.executor = executor
        self.daemon = EclairD(lightning_dir, btc.bitcoin_dir,
                              port=lightning_port)
        self.rpc = EclairRpc(
            'http://localhost:{}'.format(self.daemon.rpc_port))

    def peers(self):
        return self.rpc.peers()

    def id(self):
        info = self.rpc._call("getinfo", [])
        return info['nodeId']

    def openchannel(self, node_id, host, port, satoshis):
        return self.rpc._call('open', [node_id, host, port, satoshis, 0])

    def getaddress(self):
        return self.daemon.addr

    def addfunds(self, bitcoind, satoshis):
        addr = self.getaddress()
        bitcoind.rpc.sendtoaddress(addr, float(satoshis) / 10**8)
        self.daemon.wait_for_log('received txid=')

    def ping(self):
        """ Simple liveness test to see if the node is up and running

        Returns true if the node is reachable via RPC, false otherwise.
        """
        try:
            self.rpc.help()
            return True
        except:
            return False

    def check_channel(self, remote):
        """ Make sure that we have an active channel with remote
        """
        remote_id = remote.id()
        for c in self.rpc.channels():
            channel = self.rpc.channel(c)
            if channel['nodeid'] == remote_id:
                return channel['state'] == 'NORMAL'
        return False

    def getchannels(self):
        channels = self.rpc._call('allchannels')
        import pdb; pdb.set_trace()
        return channels

    def getnodes(self):
        return set(self.rpc.allnodes())

    def invoice(self, amount):
        addr = self.rpc._call("receive", [amount, "invoice1"])
        a = lndecode(addr)
        return hexlify(a.paymenthash).decode('ASCII')

    def send(self, other, rhash, amount):
        result = self.rpc._call("send", [amount, rhash, other.id()])
        print(result)

class EclairRpc(object):

    def __init__(self, url):
        self.url = url

    def _call(self, method, params):
        headers = {'Content-type': 'application/json'}
        data = json.dumps({'method': method, 'params': params})
        reply = requests.post(self.url, data=data, headers=headers)

        if reply.status_code != 200:
            raise ValueError("Server returned an unknown error: {}".format(
                reply.status_code))

        if 'error' in reply.json():
            raise ValueError('Error calling {}: {}'.format(
                method, reply.json()['error']))
        else:
            return reply.json()['result']

    def peers(self):
        return self._call('peers', [])

    def channels(self):
        return self._call('channels', [])

    def channel(self, cid):
        return self._call('channel', [cid])

    def allnodes(self):
        return self._call('allnodes', [])

    def help(self):
        return self._call('help', [])

    def connect(self, host, port, node_id):
        return self._call('connect', [host, port, node_id])
