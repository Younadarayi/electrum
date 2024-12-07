# Copyright (C) 2018 The Electrum developers
# Distributed under the MIT software license, see the accompanying
# file LICENCE or http://www.opensource.org/licenses/mit-license.php

from typing import NamedTuple, Iterable, TYPE_CHECKING
import os
import copy
import asyncio
from enum import IntEnum, auto
from typing import NamedTuple, Dict

from . import util
from .sql_db import SqlDB, sql
from .wallet_db import WalletDB
from .util import bfh, log_exceptions, ignore_exceptions, TxMinedInfo, random_shuffled_copy
from .address_synchronizer import AddressSynchronizer, TX_HEIGHT_LOCAL, TX_HEIGHT_UNCONF_PARENT, TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_FUTURE
from .transaction import Transaction, TxOutpoint, PartialTransaction
from .transaction import match_script_against_template
from .lnutil import WITNESS_TEMPLATE_RECEIVED_HTLC, WITNESS_TEMPLATE_OFFERED_HTLC
from .logging import Logger


if TYPE_CHECKING:
    from .network import Network
    from .lnsweep import SweepInfo
    from .lnworker import LNWallet

class ListenerItem(NamedTuple):
    # this is triggered when the lnwatcher is all done with the outpoint used as index in LNWatcher.tx_progress
    all_done : asyncio.Event
    # txs we broadcast are put on this queue so that the test can wait for them to get mined
    tx_queue : asyncio.Queue

class TxMinedDepth(IntEnum):
    """ IntEnum because we call min() in get_deepest_tx_mined_depth_for_txids """
    DEEP = auto()
    SHALLOW = auto()
    MEMPOOL = auto()
    FREE = auto()


create_sweep_txs="""
CREATE TABLE IF NOT EXISTS sweep_txs (
funding_outpoint VARCHAR(34) NOT NULL,
ctn INTEGER NOT NULL,
prevout VARCHAR(34),
tx VARCHAR
)"""

create_channel_info="""
CREATE TABLE IF NOT EXISTS channel_info (
outpoint VARCHAR(34) NOT NULL,
address VARCHAR(32),
PRIMARY KEY(outpoint)
)"""


class SweepStore(SqlDB):

    def __init__(self, path, network):
        super().__init__(network.asyncio_loop, path)

    def create_database(self):
        c = self.conn.cursor()
        c.execute(create_channel_info)
        c.execute(create_sweep_txs)
        self.conn.commit()

    @sql
    def get_sweep_tx(self, funding_outpoint, prevout):
        c = self.conn.cursor()
        c.execute("SELECT tx FROM sweep_txs WHERE funding_outpoint=? AND prevout=?", (funding_outpoint, prevout))
        return [Transaction(r[0].hex()) for r in c.fetchall()]

    @sql
    def list_sweep_tx(self):
        c = self.conn.cursor()
        c.execute("SELECT funding_outpoint FROM sweep_txs")
        return set([r[0] for r in c.fetchall()])

    @sql
    def add_sweep_tx(self, funding_outpoint, ctn, prevout, raw_tx):
        c = self.conn.cursor()
        assert Transaction(raw_tx).is_complete()
        c.execute("""INSERT INTO sweep_txs (funding_outpoint, ctn, prevout, tx) VALUES (?,?,?,?)""", (funding_outpoint, ctn, prevout, bfh(raw_tx)))
        self.conn.commit()

    @sql
    def get_num_tx(self, funding_outpoint):
        c = self.conn.cursor()
        c.execute("SELECT count(*) FROM sweep_txs WHERE funding_outpoint=?", (funding_outpoint,))
        return int(c.fetchone()[0])

    @sql
    def get_ctn(self, outpoint, addr):
        if not self._has_channel(outpoint):
            self._add_channel(outpoint, addr)
        c = self.conn.cursor()
        c.execute("SELECT max(ctn) FROM sweep_txs WHERE funding_outpoint=?", (outpoint,))
        return int(c.fetchone()[0] or 0)

    @sql
    def remove_sweep_tx(self, funding_outpoint):
        c = self.conn.cursor()
        c.execute("DELETE FROM sweep_txs WHERE funding_outpoint=?", (funding_outpoint,))
        self.conn.commit()

    def _add_channel(self, outpoint, address):
        c = self.conn.cursor()
        c.execute("INSERT INTO channel_info (address, outpoint) VALUES (?,?)", (address, outpoint))
        self.conn.commit()

    @sql
    def remove_channel(self, outpoint):
        c = self.conn.cursor()
        c.execute("DELETE FROM channel_info WHERE outpoint=?", (outpoint,))
        self.conn.commit()

    def _has_channel(self, outpoint):
        c = self.conn.cursor()
        c.execute("SELECT * FROM channel_info WHERE outpoint=?", (outpoint,))
        r = c.fetchone()
        return r is not None

    @sql
    def get_address(self, outpoint):
        c = self.conn.cursor()
        c.execute("SELECT address FROM channel_info WHERE outpoint=?", (outpoint,))
        r = c.fetchone()
        return r[0] if r else None

    @sql
    def list_channels(self):
        c = self.conn.cursor()
        c.execute("SELECT outpoint, address FROM channel_info")
        return [(r[0], r[1]) for r in c.fetchall()]


from .util import EventListener, event_listener

class LNWatcher(Logger, EventListener):

    LOGGING_SHORTCUT = 'W'

    def __init__(self, adb: 'AddressSynchronizer', network: 'Network'):

        Logger.__init__(self)
        self.adb = adb
        self.config = network.config
        self.callbacks = {} # address -> lambda: coroutine
        self.network = network
        self.register_callbacks()
        # status gets populated when we run
        self.channel_status = {}

    async def stop(self):
        self.unregister_callbacks()

    def get_channel_status(self, outpoint):
        return self.channel_status.get(outpoint, 'unknown')

    def add_channel(self, outpoint: str, address: str) -> None:
        assert isinstance(outpoint, str)
        assert isinstance(address, str)
        cb = lambda: self.check_onchain_situation(address, outpoint)
        self.add_callback(address, cb)

    async def unwatch_channel(self, address, funding_outpoint):
        self.logger.info(f'unwatching {funding_outpoint}')
        self.remove_callback(address)

    def remove_callback(self, address):
        self.callbacks.pop(address, None)

    def add_callback(self, address, callback):
        self.adb.add_address(address)
        self.callbacks[address] = callback

    @event_listener
    async def on_event_blockchain_updated(self, *args):
        await self.trigger_callbacks()

    @event_listener
    async def on_event_adb_added_verified_tx(self, adb, tx_hash):
        if adb != self.adb:
            return
        await self.trigger_callbacks()

    @event_listener
    async def on_event_adb_set_up_to_date(self, adb):
        if adb != self.adb:
            return
        await self.trigger_callbacks()

    @log_exceptions
    async def trigger_callbacks(self):
        if not self.adb.synchronizer:
            self.logger.info("synchronizer not set yet")
            return
        for address, callback in list(self.callbacks.items()):
            await callback()

    async def check_onchain_situation(self, address, funding_outpoint):
        # early return if address has not been added yet
        if not self.adb.is_mine(address):
            return
        # inspect_tx_candidate might have added new addresses, in which case we return early
        if not self.adb.is_up_to_date():
            return
        funding_txid = funding_outpoint.split(':')[0]
        funding_height = self.adb.get_tx_height(funding_txid)
        closing_txid = self.get_spender(funding_outpoint)
        closing_height = self.adb.get_tx_height(closing_txid)
        if closing_txid:
            closing_tx = self.adb.get_transaction(closing_txid)
            if closing_tx:
                keep_watching = await self.sweep_commitment_transaction(funding_outpoint, closing_tx)
            else:
                self.logger.info(f"channel {funding_outpoint} closed by {closing_txid}. still waiting for tx itself...")
                keep_watching = True
        else:
            keep_watching = True
        await self.update_channel_state(
            funding_outpoint=funding_outpoint,
            funding_txid=funding_txid,
            funding_height=funding_height,
            closing_txid=closing_txid,
            closing_height=closing_height,
            keep_watching=keep_watching)
        if not keep_watching:
            await self.unwatch_channel(address, funding_outpoint)

    async def sweep_commitment_transaction(self, funding_outpoint, closing_tx) -> bool:
        raise NotImplementedError()  # implemented by subclasses

    async def update_channel_state(self, *, funding_outpoint: str, funding_txid: str,
                                   funding_height: TxMinedInfo, closing_txid: str,
                                   closing_height: TxMinedInfo, keep_watching: bool) -> None:
        raise NotImplementedError()  # implemented by subclasses


    def get_spender(self, outpoint) -> str:
        """
        returns txid spending outpoint.
        subscribes to addresses as a side effect.
        """
        prev_txid, index = outpoint.split(':')
        spender_txid = self.adb.db.get_spent_outpoint(prev_txid, int(index))
        # discard local spenders
        tx_mined_status = self.adb.get_tx_height(spender_txid)
        if tx_mined_status.height in [TX_HEIGHT_LOCAL, TX_HEIGHT_FUTURE]:
            spender_txid = None
        if not spender_txid:
            return
        spender_tx = self.adb.get_transaction(spender_txid)
        for i, o in enumerate(spender_tx.outputs()):
            if o.address is None:
                continue
            if not self.adb.is_mine(o.address):
                self.adb.add_address(o.address)
        return spender_txid

    def inspect_tx_candidate(self, outpoint, n: int) -> Dict[str, str]:
        """
        returns a dict of spenders for a transaction of interest.
        subscribes to addresses as a side effect.
        n==0 => outpoint is a channel funding.
        n==1 => outpoint is a commitment or close output: to_local, to_remote or first-stage htlc
        n==2 => outpoint is a second-stage htlc
        """
        prev_txid, index = outpoint.split(':')
        spender_txid = self.adb.db.get_spent_outpoint(prev_txid, int(index))
        # discard local spenders
        tx_mined_status = self.adb.get_tx_height(spender_txid)
        if tx_mined_status.height in [TX_HEIGHT_LOCAL, TX_HEIGHT_FUTURE]:
            spender_txid = None
        result = {outpoint:spender_txid}
        if n == 0:
            if spender_txid is None:
                self.channel_status[outpoint] = 'open'
            elif not self.is_deeply_mined(spender_txid):
                self.channel_status[outpoint] = 'closed (%d)' % self.adb.get_tx_height(spender_txid).conf
            else:
                self.channel_status[outpoint] = 'closed (deep)'
        if spender_txid is None:
            return result
        spender_tx = self.adb.get_transaction(spender_txid)
        if n == 1:
            # if tx input is not a first-stage HTLC, we can stop recursion
            # FIXME: this is not true for anchor channels
            if len(spender_tx.inputs()) != 1:
                return result
            o = spender_tx.inputs()[0]
            witness = o.witness_elements()
            if not witness:
                # This can happen if spender_tx is a local unsigned tx in the wallet history, e.g.:
                # channel is coop-closed, outpoint is for our coop-close output, and spender_tx is an
                # arbitrary wallet-spend.
                return result
            redeem_script = witness[-1]
            if match_script_against_template(redeem_script, WITNESS_TEMPLATE_OFFERED_HTLC):
                #self.logger.info(f"input script matches offered htlc {redeem_script.hex()}")
                pass
            elif match_script_against_template(redeem_script, WITNESS_TEMPLATE_RECEIVED_HTLC):
                #self.logger.info(f"input script matches received htlc {redeem_script.hex()}")
                pass
            else:
                return result
        for i, o in enumerate(spender_tx.outputs()):
            if o.address is None:
                continue
            if not self.adb.is_mine(o.address):
                self.adb.add_address(o.address)
            elif n < 2:
                r = self.inspect_tx_candidate(spender_txid+':%d'%i, n+1)
                result.update(r)
        return result

    def get_tx_mined_depth(self, txid: str):
        if not txid:
            return TxMinedDepth.FREE
        tx_mined_depth = self.adb.get_tx_height(txid)
        height, conf = tx_mined_depth.height, tx_mined_depth.conf
        if conf > 100:
            return TxMinedDepth.DEEP
        elif conf > 0:
            return TxMinedDepth.SHALLOW
        elif height in (TX_HEIGHT_UNCONFIRMED, TX_HEIGHT_UNCONF_PARENT):
            return TxMinedDepth.MEMPOOL
        elif height in (TX_HEIGHT_LOCAL, TX_HEIGHT_FUTURE):
            return TxMinedDepth.FREE
        elif height > 0 and conf == 0:
            # unverified but claimed to be mined
            return TxMinedDepth.MEMPOOL
        else:
            raise NotImplementedError()

    def is_deeply_mined(self, txid):
        return self.get_tx_mined_depth(txid) == TxMinedDepth.DEEP


class WatchTower(LNWatcher):

    LOGGING_SHORTCUT = 'W'

    def __init__(self, network: 'Network'):
        adb = AddressSynchronizer(WalletDB('', storage=None, upgrade=True), network.config, name=self.diagnostic_name())
        adb.start_network(network)
        LNWatcher.__init__(self, adb, network)
        self.network = network
        self.sweepstore = SweepStore(os.path.join(self.network.config.path, "watchtower_db"), network)
        # this maps funding_outpoints to ListenerItems, which have an event for when the watcher is done,
        # and a queue for seeing which txs are being published
        self.tx_progress = {} # type: Dict[str, ListenerItem]

    async def stop(self):
        await super().stop()
        await self.adb.stop()

    def diagnostic_name(self):
        return "local_tower"

    async def start_watching(self):
        # I need to watch the addresses from sweepstore
        lst = await self.sweepstore.list_channels()
        for outpoint, address in random_shuffled_copy(lst):
            self.add_channel(outpoint, address)

    async def sweep_commitment_transaction(self, funding_outpoint, closing_tx):
        spenders = self.inspect_tx_candidate(funding_outpoint, 0)
        keep_watching = False
        for prevout, spender in spenders.items():
            if spender is not None:
                keep_watching |= not self.is_deeply_mined(spender)
                continue
            sweep_txns = await self.sweepstore.get_sweep_tx(funding_outpoint, prevout)
            for tx in sweep_txns:
                await self.broadcast_or_log(funding_outpoint, tx)
                keep_watching = True
        return keep_watching

    async def broadcast_or_log(self, funding_outpoint: str, tx: Transaction):
        height = self.adb.get_tx_height(tx.txid()).height
        if height != TX_HEIGHT_LOCAL:
            return
        try:
            txid = await self.network.broadcast_transaction(tx)
        except Exception as e:
            self.logger.info(f'broadcast failure: txid={tx.txid()}, funding_outpoint={funding_outpoint}: {repr(e)}')
        else:
            self.logger.info(f'broadcast success: txid={tx.txid()}, funding_outpoint={funding_outpoint}')
            if funding_outpoint in self.tx_progress:
                await self.tx_progress[funding_outpoint].tx_queue.put(tx)
            return txid

    async def get_ctn(self, outpoint, addr):
        if addr not in self.callbacks.keys():
            self.logger.info(f'watching new channel: {outpoint} {addr}')
            self.add_channel(outpoint, addr)
        return await self.sweepstore.get_ctn(outpoint, addr)

    def get_num_tx(self, outpoint):
        async def f():
            return await self.sweepstore.get_num_tx(outpoint)
        return self.network.run_from_another_thread(f())

    def list_sweep_tx(self):
        async def f():
            return await self.sweepstore.list_sweep_tx()
        return self.network.run_from_another_thread(f())

    def list_channels(self):
        async def f():
            return await self.sweepstore.list_channels()
        return self.network.run_from_another_thread(f())

    async def unwatch_channel(self, address, funding_outpoint):
        await super().unwatch_channel(address, funding_outpoint)
        await self.sweepstore.remove_sweep_tx(funding_outpoint)
        await self.sweepstore.remove_channel(funding_outpoint)
        if funding_outpoint in self.tx_progress:
            self.tx_progress[funding_outpoint].all_done.set()

    async def update_channel_state(self, *args, **kwargs):
        pass




class LNWalletWatcher(LNWatcher):

    def __init__(self, lnworker: 'LNWallet', network: 'Network'):
        self.network = network
        self.lnworker = lnworker
        LNWatcher.__init__(self, lnworker.wallet.adb, network)

    @event_listener
    async def on_event_blockchain_updated(self, *args):
        # overload parent method with cache invalidation
        # we invalidate the cache on each new block because
        # some processes affect the list of sweep transactions
        # (hold invoice preimage revealed, MPP completed, etc)
        for chan in self.lnworker.channels.values():
            chan._sweep_info.clear()
        await self.trigger_callbacks()

    def diagnostic_name(self):
        return f"{self.lnworker.wallet.diagnostic_name()}-LNW"

    @ignore_exceptions
    @log_exceptions
    async def update_channel_state(self, *, funding_outpoint: str, funding_txid: str,
                                   funding_height: TxMinedInfo, closing_txid: str,
                                   closing_height: TxMinedInfo, keep_watching: bool) -> None:
        chan = self.lnworker.channel_by_txo(funding_outpoint)
        if not chan:
            return
        chan.update_onchain_state(
            funding_txid=funding_txid,
            funding_height=funding_height,
            closing_txid=closing_txid,
            closing_height=closing_height,
            keep_watching=keep_watching)
        await self.lnworker.handle_onchain_state(chan)

    @log_exceptions
    async def sweep_commitment_transaction(self, funding_outpoint, closing_tx) -> bool:
        """This function is called when a channel was closed. In this case
        we need to check for redeemable outputs of the commitment transaction
        or spenders down the line (HTLC-timeout/success transactions).

        Returns whether we should continue to monitor."""
        chan = self.lnworker.channel_by_txo(funding_outpoint)
        if not chan:
            return False
        chan_id_for_log = chan.get_id_for_log()
        # detect who closed and get information about how to claim outputs
        sweep_info_dict = chan.sweep_ctx(closing_tx)
        #self.logger.info(f"do_breach_remedy: {[x.name for x in sweep_info_dict.values()]}")
        keep_watching = False if sweep_info_dict else not self.is_deeply_mined(closing_tx.txid())
        # create and broadcast transactions
        for prevout, sweep_info in sweep_info_dict.items():
            prev_txid, prev_index = prevout.split(':')
            name = sweep_info.name + ' ' + chan.get_id_for_log()
            if not self.adb.get_transaction(prev_txid):
                # do not keep watching if prevout does not exist
                self.logger.info(f'prevout does not exist for {name}: {prevout}')
                continue
            spender_txid = self.get_spender(prevout)
            spender_tx = self.adb.get_transaction(spender_txid) if spender_txid else None
            if spender_tx:
                # the spender might be the remote, revoked or not
                htlc_sweepinfo = chan.maybe_sweep_htlcs(closing_tx, spender_tx)
                for prevout2, htlc_sweep_info in htlc_sweepinfo.items():
                    htlc_tx_spender = self.get_spender(prevout2)
                    if htlc_tx_spender:
                        keep_watching |= not self.is_deeply_mined(htlc_tx_spender)
                    else:
                        keep_watching = True
                        await self.maybe_redeem(htlc_sweep_info)
                # extract preimage
                keep_watching |= not self.is_deeply_mined(spender_txid)
                txin_idx = spender_tx.get_input_idx_that_spent_prevout(TxOutpoint.from_str(prevout))
                if txin_idx is not None:
                    spender_txin = spender_tx.inputs()[txin_idx]
                    chan.extract_preimage_from_htlc_txin(spender_txin)
            else:
                keep_watching = True
                # broadcast or maybe update our own tx
                await self.maybe_redeem(sweep_info)
        return keep_watching


    async def maybe_redeem(self, sweep_info: 'SweepInfo') -> None:
        if not (sweep_info.cltv_abs or sweep_info.csv_delay):
            # used to control settling of htlcs onchain for testing purposes
            # careful, this prevents revocation as well
            if not self.lnworker.enable_htlc_settle_onchain:
                return
        self.lnworker.wallet.add_sweep_info(sweep_info)
