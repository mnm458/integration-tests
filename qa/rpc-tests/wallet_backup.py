#!/usr/bin/env python3
# Copyright (c) 2026 The Zcash developers
# Distributed under the MIT software license, see the accompanying
# file COPYING or https://www.opensource.org/licenses/mit-license.php .

#
# Exercises `zallet backup` and a restore (zcash/zallet#195, #49).
#
# Funds a wallet with mature transparent coinbase, takes a backup of the
# stopped wallet, erases the live database, restores from the backup, and
# checks the wallet comes back with the same account and unspent outputs.
# Also checks that backup refuses to run against a live wallet and refuses to
# overwrite an existing file.
#

import os
import shutil
import subprocess

from test_framework.test_framework import BitcoinTestFramework
from test_framework.config import ZalletArgs
from test_framework.util import (
    COINBASE_MATURITY,
    Pool,
    assert_equal,
    assert_true,
    nu_activation_all_at_1_with_ironwood,
    start_wallet,
    start_wallets,
    wallet_dir,
    zallet_binary,
    zallet_processes,
)


class WalletBackupTest(BitcoinTestFramework):

    def __init__(self):
        super().__init__()
        self.num_nodes = 1
        self.num_wallets = 1
        self.cache_behavior = 'clean'
        self.activation_heights = nu_activation_all_at_1_with_ironwood()

    def setup_wallets(self):
        zallet_args = [
            ZalletArgs(activation_heights=self.activation_heights)
            for _ in range(self.num_wallets)
        ]
        return start_wallets(
            self.num_wallets, self.options.tmpdir, zallet_args=zallet_args)

    def wallet_datadir(self):
        return wallet_dir(self.options.tmpdir, 0)

    def run_backup(self, destination, expect_success):
        # `zallet backup` is a CLI subcommand, not an RPC; invoke it the same
        # way the harness invokes init-wallet-encryption and generate-mnemonic.
        args = [zallet_binary(), "-d=" + self.wallet_datadir(),
                "backup", destination]
        result = subprocess.run(args, capture_output=True, text=True)
        if expect_success:
            assert_equal(result.returncode, 0)
        else:
            assert_true(result.returncode != 0,
                        "backup should have failed but exited 0")
        return result

    def transparent_unspent(self, wallet):
        unspent = wallet.z_listunspent(0)
        txos = [u for u in unspent if u['pool'] == Pool.TRANSPARENT]
        total = sum(u['valueZat'] for u in txos)
        return txos, total

    def run_test(self):
        node = self.nodes[0]
        w = self.wallets[0]
        backups = os.path.join(self.options.tmpdir, "backups")
        os.makedirs(backups)

        # Fund the wallet with mature transparent coinbase.
        print("Mining mature coinbase to the wallet...")
        node.generate(COINBASE_MATURITY + 2)
        self.sync_all()

        acct = w.z_listaccounts()[0]['account_uuid']
        before_txos, before_total = self.transparent_unspent(w)
        assert_true(before_total > 0,
                    "wallet should hold mature transparent coinbase")
        print("Before backup: account {}, {} transparent output(s), total {}"
              .format(acct, len(before_txos), before_total))

        # Backup must refuse while the wallet holds the datadir lock.
        print("Backup must refuse against a running wallet...")
        running = self.run_backup(backups, expect_success=False)
        assert_true(zallet_processes[0].poll() is None,
                    "the running wallet must survive a refused backup")

        # Stop the wallet so the datadir is quiesced, then back it up.
        print("Stopping the wallet and backing it up...")
        w.stop()
        zallet_processes[0].wait()

        result = self.run_backup(backups, expect_success=True)
        assert_true("pages" in result.stdout,
                    "backup should report a page count")
        files = [f for f in os.listdir(backups)
                 if f.startswith("zallet-wallet-backup-") and f.endswith(".db")]
        assert_equal(len(files), 1)
        backup_file = os.path.join(backups, files[0])

        # A second backup to the same directory rotates; an explicit existing
        # path is refused.
        self.run_backup(backup_file, expect_success=False)

        # Erase the live database, keeping the encryption identity in place,
        # and restore from the backup.
        print("Erasing the wallet database and restoring from the backup...")
        datadir = self.wallet_datadir()
        live_db = os.path.join(datadir, "wallet.db")
        for suffix in ("", "-wal", "-shm"):
            path = live_db + suffix
            if os.path.exists(path):
                os.remove(path)
        shutil.copyfile(backup_file, live_db)

        # Restart the wallet; start_wallet skips key generation because the
        # restored database is already present.
        self.wallets[0] = start_wallet(
            0, self.options.tmpdir,
            zallet_args=ZalletArgs(activation_heights=self.activation_heights))
        w = self.wallets[0]

        restored_acct = w.z_listaccounts()[0]['account_uuid']
        after_txos, after_total = self.transparent_unspent(w)
        print("After restore: account {}, {} transparent output(s), total {}"
              .format(restored_acct, len(after_txos), after_total))

        assert_equal(restored_acct, acct)
        assert_equal(after_total, before_total)
        assert_equal(len(after_txos), len(before_txos))
        print("PASSED: backup and restore preserved the account and funds")


if __name__ == '__main__':
    WalletBackupTest().main()
