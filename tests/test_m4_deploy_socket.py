"""Unix Socket lifecycle safeguards; temporary files only, no Flow import."""
import importlib.util
from pathlib import Path
import socket
import stat
import tempfile
import unittest
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1] / 'm4/deploy/backend/entrypoint.py'
spec = importlib.util.spec_from_file_location('m4_deploy_entrypoint', SOURCE)
entrypoint = importlib.util.module_from_spec(spec)
spec.loader.exec_module(entrypoint)


class SocketLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='m4-socket-', dir='/tmp')
        self.directory = Path(self.temp.name)
        self.path = self.directory / 'api.sock'
        self.patches = [patch.object(entrypoint, 'SOCKET_ROOT', self.directory),
                        patch.object(entrypoint, 'SOCKET_PATH', self.path)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in self.patches:
            item.stop()
        self.temp.cleanup()

    def test_stale_socket_replaced_and_owned_socket_removed_on_exit(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as old:
            old.bind(str(self.path))
        with entrypoint.socket_listener():
            info = self.path.stat()
            self.assertTrue(stat.S_ISSOCK(info.st_mode))
            self.assertEqual(stat.S_IMODE(info.st_mode), 0o660)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(self.path))
        self.assertFalse(self.path.exists())

    def test_running_instance_lock_does_not_replace_live_socket(self):
        with entrypoint.socket_listener():
            inode = self.path.stat().st_ino
            with self.assertRaises(OSError):
                with entrypoint.socket_listener():
                    self.fail('second instance started')
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_existing_unlocked_active_socket_not_replaced(self):
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as existing:
            existing.bind(str(self.path))
            existing.listen(1)
            inode = self.path.stat().st_ino
            with self.assertRaises(ValueError):
                with entrypoint.socket_listener():
                    self.fail('active socket replaced')
            self.assertEqual(self.path.stat().st_ino, inode)

    def test_regular_file_and_symlink_not_removed(self):
        self.path.write_text('preserve')
        with self.assertRaises(ValueError):
            with entrypoint.socket_listener():
                self.fail('regular file replaced')
        self.assertEqual(self.path.read_text(), 'preserve')
        self.path.unlink()
        target = self.directory / 'target'
        target.write_text('preserve')
        self.path.symlink_to(target)
        with self.assertRaises(ValueError):
            with entrypoint.socket_listener():
                self.fail('symlink replaced')
        self.assertTrue(self.path.is_symlink())
        self.assertEqual(target.read_text(), 'preserve')

    def test_symlink_socket_directory_rejected(self):
        linked = self.directory / 'linked'
        linked.symlink_to(self.directory, target_is_directory=True)
        with patch.object(entrypoint, 'SOCKET_ROOT', linked), self.assertRaises(ValueError):
            entrypoint.prepare_socket_directory()


if __name__ == '__main__':
    unittest.main()
