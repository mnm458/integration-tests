#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Zallet's `builder.tx_expiry_delta` option, against the Z3 stack
# (zebrad + zallet).
#
# A transaction is built to expire a number of blocks after its target height,
# the first block it could be mined in (ZIP 203). With the option unset that
# number is the network's default; with it set, it is the configured value
# (zcash/zallet#200). This test checks both, for every RPC that builds a
# transaction: `z_shieldcoinbase`, `z_sendmany` and `pczt_create`.
#

from decimal import Decimal

from test_framework.config import ZalletArgs
from test_framework.test_framework import BitcoinTestFramework
from test_framework.util import (
    COIN,
    DEFAULT_TX_EXPIRY_DELTA,
    INTERNAL_FEE,
    MIN_CONFIRMATIONS,
    Pool,
    PrivacyPolicy,
    Receiver,
    assert_equal,
    first_transparent_receiver,
    shield_coinbase,
    start_wallets,
    stop_wallets,
    unified_address_for,
    wait_and_assert_operationid_status,
    wait_for_account_spendable,
    wait_for_tx_scanned,
    wait_for_wallet_sync,
    wait_zallets,
)

# The expiry delta the wallet is configured with. Well away from the default, so
# that a transaction built with one cannot be mistaken for the other.
TX_EXPIRY_DELTA = 10

# Diversifier indices of the addresses the test uses: the shielded address that
# holds the funds, the shielded address payments are sent to, and the address
# whose transparent receiver is paid.
DIVERSIFIER_FUNDS = 1000
DIVERSIFIER_SHIELDED_RECIPIENT = 1001
DIVERSIFIER_TRANSPARENT_RECIPIENT = 1002

# Value of each payment. Small next to a shielded coinbase, so the funds cover
# every payment in the test.
PAYMENT_AMOUNT = Decimal('1')

# The least an account must be able to spend before the next payment is built:
# the payment and room for its fee.
MIN_SPENDABLE_ZAT = 2 * int(PAYMENT_AMOUNT * COIN)


class WalletTxExpiryDeltaTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'

    def restart_wallet(self, zallet_args=None):
        """Stop and restart zallet on the same wallet database, applying
        `zallet_args` if given, and block until it has caught up with the node."""
        stop_wallets(self.wallets)
        wait_zallets()
        self.wallets = start_wallets(
            self.num_wallets, self.options.tmpdir,
            zallet_args=None if zallet_args is None else [zallet_args])
        wait_for_wallet_sync(self.nodes[0], self.wallets[0])
        return self.wallets[0]

    def target_height(self):
        """The target height of a transaction the wallet builds now: the block
        after the tip. The wallet must be in sync with the node."""
        return self.nodes[0].getblockcount() + 1

    def assert_expires_after(self, txid, target_height, delta):
        """Check that the mined transaction `txid` was mined at `target_height`
        and expires `delta` blocks after it."""
        tx = self.nodes[0].getrawtransaction(txid, 1)
        assert_equal(target_height, tx['height'],
                     "the transaction must be mined at its target height")
        assert_equal(target_height + delta, tx['expiryheight'])

    def send(self, wallet, account_uuid, from_addr, to_addr, privacy_policy):
        """Pay `PAYMENT_AMOUNT` from `from_addr` to `to_addr`, mine it, and
        return `(txid, target_height)`."""
        node = self.nodes[0]
        wait_for_wallet_sync(node, wallet)
        wait_for_account_spendable(
            wallet, account_uuid, Pool.ORCHARD, min_zat=MIN_SPENDABLE_ZAT)
        target_height = self.target_height()
        opid = wallet.z_sendmany(
            from_addr,
            [{'address': to_addr, 'amount': PAYMENT_AMOUNT}],
            MIN_CONFIRMATIONS,
            INTERNAL_FEE,
            privacy_policy)
        txid = wait_and_assert_operationid_status(wallet, opid)
        node.generate(1)
        wait_for_tx_scanned(wallet, txid)
        return txid, target_height

    def shield(self, wallet, account_uuid, to_addr):
        """Shield coinbase into `to_addr`, returning `(txid, target_height)`.
        `shield_coinbase` mines the transaction in the block after the one the
        wallet built it on, so its target height is the height it was mined at."""
        node = self.nodes[0]
        txid, _ = shield_coinbase(
            node, wallet, self.miner_addresses[0], to_addr, account_uuid,
            Pool.ORCHARD)
        return txid, node.getrawtransaction(txid, 1)['height']

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]

        self.sync_all()
        acct = w.z_listaccounts()[0]['account_uuid']
        ua_funds = unified_address_for(
            w, acct, DIVERSIFIER_FUNDS, [Receiver.ORCHARD])
        ua_recipient = unified_address_for(
            w, acct, DIVERSIFIER_SHIELDED_RECIPIENT, [Receiver.ORCHARD])
        taddr_recipient = first_transparent_receiver(w, unified_address_for(
            w, acct, DIVERSIFIER_TRANSPARENT_RECIPIENT,
            [Receiver.P2PKH, Receiver.ORCHARD]))

        # ---- Phase 1: unset, the network's default applies ----------------
        print("Phase 1: with the option unset, transactions expire %d blocks "
              "after their target height" % DEFAULT_TX_EXPIRY_DELTA)
        txid, target = self.shield(w, acct, ua_funds)
        self.assert_expires_after(txid, target, DEFAULT_TX_EXPIRY_DELTA)
        txid, target = self.send(
            w, acct, ua_funds, ua_recipient, PrivacyPolicy.FULL_PRIVACY)
        self.assert_expires_after(txid, target, DEFAULT_TX_EXPIRY_DELTA)

        # ---- Phase 2: set, the configured delta applies -------------------
        print("Phase 2: restart with tx_expiry_delta = %d" % TX_EXPIRY_DELTA)
        w = self.restart_wallet(ZalletArgs(tx_expiry_delta=TX_EXPIRY_DELTA))

        print("  z_shieldcoinbase")
        txid, target = self.shield(w, acct, ua_funds)
        self.assert_expires_after(txid, target, TX_EXPIRY_DELTA)

        print("  z_sendmany to a shielded recipient")
        txid, target = self.send(
            w, acct, ua_funds, ua_recipient, PrivacyPolicy.FULL_PRIVACY)
        self.assert_expires_after(txid, target, TX_EXPIRY_DELTA)

        print("  z_sendmany to a transparent recipient")
        txid, target = self.send(
            w, acct, ua_funds, taddr_recipient,
            PrivacyPolicy.ALLOW_REVEALED_RECIPIENTS)
        self.assert_expires_after(txid, target, TX_EXPIRY_DELTA)

        # The PCZT is not completed: the expiry height is fixed when it is
        # created, which is the step the option applies to.
        print("  pczt_create")
        wait_for_wallet_sync(node, w)
        wait_for_account_spendable(
            w, acct, Pool.ORCHARD, min_zat=MIN_SPENDABLE_ZAT)
        target = self.target_height()
        pczt = w.pczt_create(
            ua_funds,
            [{'address': ua_recipient, 'amount': PAYMENT_AMOUNT}],
            MIN_CONFIRMATIONS,
            PrivacyPolicy.FULL_PRIVACY)['pczt']
        assert_equal(target + TX_EXPIRY_DELTA,
                     w.pczt_inspect(pczt)['expiry_height'])

        # ---- Phase 3: the setting survives a restart ----------------------
        # It stays in the config file, so no arguments are needed.
        print("Phase 3: restart, the configured delta still applies")
        w = self.restart_wallet()
        txid, target = self.send(
            w, acct, ua_funds, ua_recipient, PrivacyPolicy.FULL_PRIVACY)
        self.assert_expires_after(txid, target, TX_EXPIRY_DELTA)

        print("\nAll transaction expiry delta tests passed!")


if __name__ == '__main__':
    WalletTxExpiryDeltaTest().main()
