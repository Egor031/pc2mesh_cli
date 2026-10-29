import contextlib
import csv
import ctypes
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import open3d as o3d


REPO = Path(__file__).resolve().parents[1]
SCRIPTS = REPO / "pc2mesh_cli" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import file_safety as fs
import reconstruct_poisson as app
from obj_oracle import assert_exact, read_obj


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dacl_bytes(descriptor):
    # A self-relative SECURITY_DESCRIPTOR contains the DACL offset at byte 16.
    # AUTO_INHERITED bookkeeping can change; ACE bytes and protection must not.
    offset = struct.unpack_from("<I", descriptor.raw, 16)[0]
    size = struct.unpack_from("<H", descriptor.raw, offset + 2)[0]
    return descriptor.raw[offset:offset + size]


@contextlib.contextmanager
def no_delete_share(path):
    if os.name != "nt":
        raise unittest.SkipTest("Windows sharing test")
    handle = fs._create_file(str(path), 0x80000000, 1, None, 3, 0x80, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        yield
    finally:
        fs._close_handle(handle)


class TemporaryFiles(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="pc2mesh-01a-test-")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.source = self.root / "source.ply"
        self.output = self.root / "result.obj"
        self.log = self.root / "run.log"
        self.source.write_bytes(b"input sentinel\x00\xff")
        self.output.write_bytes(b"previous output\x00\xff")
        self.log.write_bytes(b"previous log\x00\xff")
        self.initial = {p: digest(p) for p in (self.source, self.output, self.log)}

    def unchanged(self, *paths):
        for path in paths:
            self.assertEqual(digest(path), self.initial[path], str(path))

    def no_temporary_files(self):
        self.assertEqual(list(self.root.rglob(".pc2mesh-*")), [])

    def alias(self, source, kind):
        alias = self.root / ("alias-" + kind)
        if kind == "hardlink":
            os.link(source, alias)
        else:
            try:
                alias.symlink_to(source)
            except OSError as ex:
                if getattr(ex, "winerror", None) == 1314:
                    self.skipTest("Symlink creation privilege is unavailable")
                raise
        return alias

    @contextlib.contextmanager
    def deny_directory_writes(self, directory):
        if os.name != "nt" or shutil.which("icacls") is None:
            self.skipTest("Windows icacls is unavailable")
        directory = directory.resolve()
        self.assertTrue(directory.is_relative_to(self.root.resolve()))
        user = subprocess.run(["whoami", "/user", "/fo", "csv", "/nh"],
                              capture_output=True, check=True)
        sid = next(csv.reader([user.stdout.decode(errors="replace").strip()]))[1]
        result = subprocess.run(["icacls", str(directory), "/deny", f"*{sid}:(WD,AD,DC)"],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        try:
            yield
        finally:
            restored = subprocess.run(["icacls", str(directory), "/remove:d", f"*{sid}"],
                                      capture_output=True)
            self.assertEqual(restored.returncode, 0, restored.stderr)


class PathTests(TemporaryFiles):
    def test_same_and_normalized_paths(self):
        (self.root / "sub").mkdir()
        for other in (self.source, self.root / "sub" / ".." / self.source.name):
            with self.subTest(other=other), self.assertRaisesRegex(ValueError, "Path conflict"):
                fs.ensure_distinct_paths({"input": self.source, "output": other})
        self.unchanged(self.source)

    @unittest.skipUnless(os.name == "nt", "Windows case aliases")
    def test_case_and_nonexistent_case_alias(self):
        for source in (self.source, self.root / "not-created.obj"):
            with self.subTest(source=source), self.assertRaises(ValueError):
                fs.ensure_distinct_paths({"input": source, "output": str(source).upper()})

    def test_hardlink(self):
        with self.assertRaises(ValueError):
            fs.ensure_distinct_paths({"input": self.source, "output": self.alias(self.source, "hardlink")})
        self.unchanged(self.source)

    def test_symlink(self):
        with self.assertRaises(ValueError):
            fs.ensure_distinct_paths({"input": self.source, "log": self.alias(self.source, "symlink")})
        self.unchanged(self.source)

    @unittest.skipUnless(os.name == "nt", "Windows junction")
    def test_parent_junction_for_missing_outputs(self):
        real = self.root / "real"
        real.mkdir()
        alias = self.root / "junction"
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(alias), str(real)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with self.assertRaises(ValueError):
            fs.ensure_distinct_paths({"output": real / "missing.obj", "log": alias / "missing.obj"})

    @unittest.skipUnless(os.name == "nt", "Windows short names")
    def test_short_file_alias(self):
        long = self.root / "a sufficiently long source filename.ply"
        long.write_bytes(b"short name fixture")
        get_short = fs._kernel32.GetShortPathNameW
        get_short.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
        get_short.restype = ctypes.c_uint32
        buffer = ctypes.create_unicode_buffer(32768)
        if not get_short(str(long), buffer, len(buffer)):
            self.skipTest("8.3 file alias is unavailable")
        if Path(buffer.value).name.casefold() == long.name.casefold():
            self.skipTest("8.3 name generation is disabled")
        with self.assertRaises(ValueError):
            fs.ensure_distinct_paths({"input": long, "output": buffer.value})

    @unittest.skipUnless(os.name == "nt", "Windows special paths")
    def test_devices_and_alternate_streams_rejected(self):
        for name in ("NUL", "CON.txt", str(self.source) + ":stream", r"\\.\pipe\not-an-output"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                fs.ensure_distinct_paths({"output": name})

    @unittest.skipUnless(os.name == "nt", "Windows input sharing")
    def test_input_lock_blocks_writes_through_hardlink(self):
        alias = self.alias(self.source, "hardlink")
        with fs.InputFileLock(self.source) as lock:
            lock.check()
            with self.assertRaises(OSError):
                alias.write_bytes(b"must not truncate")
        self.unchanged(self.source)


class AtomicOutputTests(TemporaryFiles):
    def test_temporary_creation_fails_fast_and_retries_only_collisions(self):
        with patch.object(fs.os, "open", side_effect=PermissionError("ACL denied")) as create:
            with self.assertRaises(PermissionError):
                fs.AtomicOutput(self.output)
            self.assertEqual(create.call_count, 1)
        with patch.object(fs.os, "open", side_effect=FileExistsError("name collision")) as create:
            with self.assertRaises(FileExistsError):
                fs.AtomicOutput(self.output)
            self.assertLessEqual(create.call_count, 64)
        self.unchanged(self.output)
        self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows DACL")
    def test_success_preserves_existing_dacl(self):
        subprocess.run(["icacls", str(self.output), "/inheritance:d"], check=True, capture_output=True)
        subprocess.run(["icacls", str(self.output), "/deny", "*S-1-5-32-546:(W)"], check=True, capture_output=True)
        before, flags = fs._permissions(self.output)
        with fs.AtomicOutput(self.output) as staged:
            staged.stream.write("new output")
            fs.publish_outputs([staged], lambda: None)
        after, after_flags = fs._permissions(self.output)
        self.assertEqual(dacl_bytes(before), dacl_bytes(after))
        self.assertEqual(flags, after_flags)
        self.no_temporary_files()

    def test_success_and_repeat(self):
        for contents in ("first", "second"):
            with fs.AtomicOutput(self.output) as staged:
                staged.stream.write(contents)
                self.assertNotEqual(self.output.read_text(errors="replace"), contents)
                fs.publish_outputs([staged], lambda: None)
            self.assertEqual(self.output.read_text(), contents)
        self.no_temporary_files()

    def test_partial_write_failure(self):
        with self.assertRaises(OSError):
            with fs.AtomicOutput(self.output) as staged:
                staged.stream.write("partial")
                raise OSError("simulated ENOSPC after partial write")
        self.unchanged(self.output)
        self.no_temporary_files()

    def test_flush_sync_close_failures(self):
        for operation in ("flush", "sync", "close"):
            with self.subTest(operation=operation):
                with self.assertRaises(OSError), fs.AtomicOutput(self.output) as staged:
                    staged.stream.write("complete")
                    if operation == "sync":
                        with patch.object(fs.os, "fsync", side_effect=OSError("sync failed")):
                            staged.finish()
                    else:
                        real = staged.stream
                        class FailingStream:
                            def __getattr__(self, name):
                                return getattr(real, name)

                            def flush(self):
                                real.flush()
                                if operation == "flush":
                                    raise OSError("flush failed")

                            def close(self):
                                real.close()
                                if operation == "close":
                                    raise OSError("close failed")

                        staged.stream = FailingStream()
                        staged.finish()
                self.unchanged(self.output)
                self.no_temporary_files()

    def test_open_and_publish_failure(self):
        with self.assertRaises(OSError):
            fs.AtomicOutput(self.root / "missing" / "out.obj")
        with self.assertRaises(OSError), fs.AtomicOutput(self.output) as staged:
            staged.stream.write("complete")
            staged.finish()
            with patch.object(fs, "replace_file", side_effect=OSError("publication failed")):
                staged.publish()
        self.unchanged(self.output)
        self.no_temporary_files()

    def test_target_changed_before_publication(self):
        with self.assertRaises(OSError), fs.AtomicOutput(self.output) as staged:
            staged.stream.write("new result")
            staged.finish()
            self.output.write_bytes(b"concurrent replacement")
            staged.publish()
        self.assertEqual(self.output.read_bytes(), b"concurrent replacement")
        self.no_temporary_files()

    def test_recheck_after_alias_created(self):
        target = self.root / "new.obj"
        validate = lambda: fs.ensure_distinct_paths({"input": self.source, "output": target})
        with self.assertRaises(ValueError), fs.AtomicOutput(target) as staged:
            staged.stream.write("new result")
            os.link(self.source, target)
            fs.publish_outputs([staged], validate)
        self.unchanged(self.source)
        self.assertEqual(digest(target), self.initial[self.source])
        self.no_temporary_files()

    def test_log_publication_failure_rolls_back_output(self):
        original_replace = fs.replace_file
        def replace(source, target):
            if target == self.log:
                raise OSError("log publication failed")
            return original_replace(source, target)

        for existed in (True, False):
            with self.subTest(existed=existed):
                if not existed:
                    self.output.unlink()
                with self.assertRaises(OSError), fs.AtomicOutput(self.output) as mesh, fs.AtomicOutput(self.log) as log:
                    mesh.stream.write("new mesh")
                    log.stream.write("new log")
                    with patch.object(fs, "replace_file", side_effect=replace):
                        fs.publish_outputs([mesh, log], lambda: None)
                self.unchanged(self.log)
                if existed:
                    self.unchanged(self.output)
                else:
                    self.assertFalse(self.output.exists())
                self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows sharing")
    def test_real_sharing_violation(self):
        with no_delete_share(self.output):
            with self.assertRaises(OSError), fs.AtomicOutput(self.output) as staged:
                staged.stream.write("complete")
                fs.publish_outputs([staged], lambda: None)
        self.unchanged(self.output)
        self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows file attributes")
    def test_readonly_existing_output(self):
        self.output.chmod(stat.S_IREAD)
        try:
            with self.assertRaises(OSError), fs.AtomicOutput(self.output) as staged:
                staged.stream.write("new output")
                fs.publish_outputs([staged], lambda: None)
        finally:
            self.output.chmod(stat.S_IREAD | stat.S_IWRITE)
        self.unchanged(self.output)
        self.no_temporary_files()

    def test_rollback_failure_retains_recoverable_old_bytes(self):
        original_replace = fs.replace_file
        count = 0
        def replace(source, target):
            nonlocal count
            count += 1
            if count >= 2:
                raise OSError("simulated log and rollback lock")
            return original_replace(source, target)
        with self.assertRaisesRegex(OSError, "rollback failed"), fs.AtomicOutput(self.output) as mesh, fs.AtomicOutput(self.log) as log:
            mesh.stream.write("new mesh")
            log.stream.write("new log")
            with patch.object(fs, "replace_file", side_effect=replace):
                fs.publish_outputs([mesh, log], lambda: None)
        backups = list(self.root.glob(".pc2mesh-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(digest(backups[0]), self.initial[self.output])
        self.unchanged(self.log)
        original_replace(backups[0], self.output)
        self.unchanged(self.output)
        self.no_temporary_files()

    def test_committed_results_remain_successful_if_backup_cleanup_is_blocked(self):
        unlink = Path.unlink
        def fail_backup(path, *args, **kwargs):
            if path.suffix == ".bak":
                raise PermissionError("backup locked by another process")
            return unlink(path, *args, **kwargs)
        errors = io.StringIO()
        with fs.AtomicOutput(self.output) as mesh, fs.AtomicOutput(self.log) as log:
            mesh.stream.write("new mesh")
            log.stream.write("new log")
            with patch.object(Path, "unlink", fail_backup), contextlib.redirect_stderr(errors):
                fs.publish_outputs([mesh, log], lambda: None)
        self.assertEqual(self.output.read_text(), "new mesh")
        self.assertEqual(self.log.read_text(), "new log")
        self.assertIn("WARNING", errors.getvalue())
        for backup in self.root.glob(".pc2mesh-*.bak"):
            self.assertEqual(digest(backup), self.initial[self.output])
            unlink(backup)
        self.no_temporary_files()


class ReconstructionTests(TemporaryFiles):
    def setUp(self):
        super().setUp()
        index = np.arange(400)
        z = 1.0 - 2.0 * (index + 0.5) / len(index)
        angle = index * np.pi * (3.0 - np.sqrt(5.0))
        radius = np.sqrt(1.0 - z * z)
        self.points = np.column_stack((radius * np.cos(angle), radius * np.sin(angle), z))
        cloud = o3d.geometry.PointCloud(o3d.utility.Vector3dVector(self.points))
        self.assertTrue(o3d.io.write_point_cloud(str(self.source), cloud, write_ascii=True))
        self.initial[self.source] = digest(self.source)
        self.mesh = o3d.geometry.TriangleMesh.create_sphere(radius=0.5, resolution=5)

    def main(self, source=None, output=None, log=None, real_poisson=False):
        arguments = [str(SCRIPTS / "reconstruct_poisson.py"), str(source or self.source), str(output or self.output)]
        if log is not False:
            arguments.append(str(log or self.log))
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(sys, "argv", arguments))
            stack.enter_context(contextlib.redirect_stdout(stdout))
            stack.enter_context(contextlib.redirect_stderr(stderr))
            if not real_poisson:
                stack.enter_context(patch.object(o3d.geometry.TriangleMesh, "create_from_point_cloud_poisson",
                    return_value=(o3d.geometry.TriangleMesh(self.mesh), np.ones(len(self.mesh.vertices)))))
            result = app.main()
        self.no_temporary_files()
        return result, stdout.getvalue(), stderr.getvalue()

    def test_all_path_collisions_leave_all_bytes_unchanged(self):
        for source, output, log in ((self.source, self.source, self.log),
                                    (self.source, self.output, self.source),
                                    (self.source, self.output, self.output)):
            with self.subTest(source=source, output=output, log=log):
                rc, out, err = self.main(source, output, log)
                self.assertEqual(rc, 1)
                self.assertIn("Path conflict", err)
                self.assertNotIn("Status: OK", out)
                self.unchanged(self.source, self.output, self.log)

    def test_alias_collisions(self):
        for kind in ("hardlink", "symlink"):
            with self.subTest(kind=kind):
                alias = self.alias(self.source, kind)
                for output, log in ((alias, self.log), (self.output, alias)):
                    rc, out, err = self.main(output=output, log=log)
                    self.assertEqual(rc, 1)
                    self.assertIn("Path conflict", err)
                    self.unchanged(self.source, self.output, self.log)
                alias.unlink()
                alias = self.alias(self.output, kind)
                rc, out, err = self.main(log=alias)
                self.assertEqual(rc, 1)
                self.assertIn("Path conflict", err)
                self.unchanged(self.source, self.output, self.log)

    def test_normalized_aliases_for_all_pairs(self):
        (self.root / "sub").mkdir()
        for a, b in (("input", "output"), ("input", "log"), ("output", "log")):
            paths = {"input": self.source, "output": self.output, "log": self.log}
            base = paths[a]
            variants = [self.root / "sub" / ".." / base.name]
            if os.name == "nt":
                variants.append(str(base).upper())
            for alias in variants:
                with self.subTest(a=a, b=b, alias=alias):
                    paths[b] = alias
                    rc, out, err = self.main(paths["input"], paths["output"], paths["log"])
                    self.assertEqual(rc, 1)
                    self.assertIn("Path conflict", err)
                    self.unchanged(self.source, self.output, self.log)

    def test_partial_mesh_write_preserves_output_and_saves_error_log(self):
        def partial(stream, *args):
            stream.write("partial mesh")
            raise OSError("simulated write failure after partial output")
        with patch.object(app, "write_obj", side_effect=partial):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertNotIn("Status: OK", out)
        self.unchanged(self.source, self.output)
        self.assertIn("Status: ERROR", self.log.read_text())
        self.assertIn("partial output", err)

    def test_failed_log_write_preserves_previous_log_and_output(self):
        finish = fs.AtomicOutput.finish
        def fail_log(staged):
            if staged.target == self.log:
                raise OSError("log write/close failure")
            return finish(staged)
        with patch.object(fs.AtomicOutput, "finish", fail_log):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertNotIn("Status: OK", out)
        self.unchanged(self.source, self.output, self.log)

    @unittest.skipUnless(os.name == "nt", "Windows sharing")
    def test_busy_log_rolls_back_mesh_without_false_ok(self):
        with no_delete_share(self.log):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertNotIn("Status: OK", out)
        self.unchanged(self.source, self.output, self.log)

    def test_missing_input_and_unavailable_parent(self):
        rc, out, err = self.main(source=self.root / "missing.ply")
        self.assertEqual(rc, 1)
        self.unchanged(self.source, self.output)
        self.assertIn("Status: ERROR", self.log.read_text())
        parent = self.root / "a-file"
        parent.write_bytes(b"parent sentinel")
        rc, out, err = self.main(output=parent / "out.obj")
        self.assertEqual(rc, 1)
        self.assertNotIn("Traceback", err)
        self.assertEqual(parent.read_bytes(), b"parent sentinel")
        self.unchanged(self.source, self.output)

    @unittest.skipUnless(os.name == "nt", "Windows ACL")
    def test_access_denied_preserves_output(self):
        protected = self.root / "denied"
        protected.mkdir()
        target = protected / "old.obj"
        target.write_bytes(b"protected old output")
        before = digest(target)
        with self.deny_directory_writes(protected):
            rc, out, err = self.main(output=target)
        self.assertEqual(rc, 1)
        self.assertNotIn("Status: OK", out)
        self.assertEqual(digest(target), before)
        self.assertIn("Status: ERROR", self.log.read_text())
        self.unchanged(self.source)
        self.no_temporary_files()

    def test_partial_log_write_preserves_all_previous_files(self):
        def make_output(path, **kwargs):
            staged = fs.AtomicOutput(path, **kwargs)
            if staged.target == self.log:
                real = staged.stream
                class PartialLog:
                    def __getattr__(self, name):
                        return getattr(real, name)

                    def write(self, text):
                        real.write(text[:32])
                        raise OSError("log write interrupted after 32 characters")
                staged.stream = PartialLog()
            return staged
        with patch.object(app, "AtomicOutput", side_effect=make_output):
            rc, out, err = self.main()
        self.assertEqual(rc, 1)
        self.assertNotIn("Status: OK", out)
        self.unchanged(self.source, self.output, self.log)
        self.no_temporary_files()

    def test_obj_payload_preserves_double_precision_and_attributes(self):
        self.mesh.compute_vertex_normals()
        self.mesh.paint_uniform_color([0.1, 0.2, 0.3])
        baseline = self.root / "baseline.obj"
        self.assertTrue(o3d.io.write_triangle_mesh(str(baseline), self.mesh, write_ascii=True))
        buffer = io.StringIO()
        app.write_obj(buffer, self.mesh, baseline.stem)
        actual = read_obj(buffer.getvalue())
        for name, values in (("vertices", self.mesh.vertices), ("normals", self.mesh.vertex_normals),
                             ("colors", self.mesh.vertex_colors)):
            assert_exact(self, np.asarray(values), actual[name])
        np.testing.assert_array_equal(np.asarray(self.mesh.triangles), actual["triangles"])
        np.testing.assert_array_equal(actual["triangles"], actual["normal_indices"])
        # Precision changes intentionally; retain the previous header, record
        # order and face syntax rather than comparing six-digit numeric bytes.
        native = baseline.read_text().splitlines()
        precise = buffer.getvalue().splitlines()
        self.assertEqual(native[:4], precise[:4])
        self.assertEqual([line.split()[0] for line in native], [line.split()[0] for line in precise])
        self.assertEqual([line for line in native if line.startswith("f ")],
                         [line for line in precise if line.startswith("f ")])

    def test_real_poisson_success_replacement_spaces_and_other_cwd(self):
        space = self.root / "space folder"
        space.mkdir()
        target = space / "mesh.obj"
        journal = space / "log.txt"
        target.write_bytes(b"old mesh")
        previous_cwd = Path.cwd()
        try:
            os.chdir(self.root)
            rc, out, err = self.main(Path("source.ply"), Path("space folder/mesh.obj"),
                                     Path("space folder/log.txt"), real_poisson=True)
        finally:
            os.chdir(previous_cwd)
        self.assertEqual(rc, 0, err)
        self.assertIn("Status: OK", out)
        self.assertIn("Status: OK", journal.read_text())
        mesh = o3d.io.read_triangle_mesh(str(target))
        self.assertGreater(len(mesh.triangles), 0)
        vertices = np.asarray(mesh.vertices)
        self.assertTrue(np.isfinite(vertices).all())
        self.assertLess(float(np.abs(np.linalg.norm(vertices, axis=1) - 1).max()), 0.15)
        self.unchanged(self.source)
        rc, out, err = self.main(output=target, log=journal)
        self.assertEqual(rc, 0, err)


@unittest.skipUnless(os.environ.get("PC2MESH_TEST_DRIVER"), "Set PC2MESH_TEST_DRIVER to the compiled C++ test driver")
class CppTests(TemporaryFiles):
    def driver(self, mode, output=None, source=None):
        command = [os.environ["PC2MESH_TEST_DRIVER"], mode, str(output or self.output)]
        if source is not None:
            command.append(str(source))
        return subprocess.run(command, cwd=self.root, capture_output=True, timeout=30)

    def test_open_write_flush_sync_close_publish_errors(self):
        for mode in ("write_error", "flush_error", "sync_error", "close_error", "publish_error"):
            with self.subTest(mode=mode):
                result = self.driver(mode, source=self.source)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertNotIn(b"Done", result.stdout)
                self.assertIn(b"ERROR", result.stderr)
                self.unchanged(self.source, self.output)
                self.no_temporary_files()
        result = self.driver("write", self.root / "absent" / "output.xyz")
        self.assertEqual(result.returncode, 1)
        self.no_temporary_files()

    def test_ply_xyz_payload_and_repeat(self):
        for mode in ("ply", "xyz"):
            target = self.root / ("space result." + mode)
            for repetition in range(2):
                result = self.driver(mode, target)
                self.assertEqual(result.returncode, 0, result.stderr)
                payload = b"1.250000000 -2.500000000 3.000000000\r\n4.000000000 5.000000000 -6.000000000\r\n"
                expected = payload if mode == "xyz" else (
                    b"ply\r\nformat ascii 1.0\r\nelement vertex 2\r\nproperty double x\r\n"
                    b"property double y\r\nproperty double z\r\nend_header\r\n" + payload)
                self.assertEqual(target.read_bytes(), expected)
            self.no_temporary_files()

    def test_identity_aliases(self):
        (self.root / "sub").mkdir()
        candidates = [self.source, self.root / "sub" / ".." / self.source.name]
        if os.name == "nt":
            candidates.append(str(self.source).upper())
        candidates.append(self.alias(self.source, "hardlink"))
        for candidate in candidates:
            with self.subTest(candidate=candidate):
                result = self.driver("ply", candidate, self.source)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.unchanged(self.source)
                self.no_temporary_files()

    def test_symlink_alias(self):
        result = self.driver("ply", self.alias(self.source, "symlink"), self.source)
        self.assertEqual(result.returncode, 1)
        self.unchanged(self.source)
        self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows junction and short names")
    def test_parent_junction_alias(self):
        alias = self.root / "junction"
        actual = self.root / "actual"
        actual.mkdir()
        real = actual / "input.ply"
        real.write_bytes(b"junction fixture")
        result = subprocess.run([os.environ["COMSPEC"], "/d", "/c", "mklink", "/J", str(alias), str(actual)],
                                capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.driver("check", alias / real.name, real)
        self.assertEqual(result.returncode, 1)
        result = self.driver("check", alias / "future.ply", actual / "future.ply")
        self.assertEqual(result.returncode, 1)
        self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows sharing and ACL")
    def test_real_lock_readonly_and_access_denied(self):
        with no_delete_share(self.output):
            result = self.driver("ply", source=self.source)
        self.assertEqual(result.returncode, 1)
        self.unchanged(self.source, self.output)
        self.output.chmod(stat.S_IREAD)
        try:
            result = self.driver("xyz", source=self.source)
            self.assertEqual(result.returncode, 1)
        finally:
            self.output.chmod(stat.S_IREAD | stat.S_IWRITE)
        protected = self.root / "denied"
        protected.mkdir()
        target = protected / "old.ply"
        target.write_bytes(b"old result")
        before = digest(target)
        with self.deny_directory_writes(protected):
            result = self.driver("ply", target, self.source)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(digest(target), before)
        self.unchanged(self.source, self.output)
        self.no_temporary_files()

    def test_identity_rechecked_before_publish(self):
        target = self.root / "new.xyz"
        result = self.driver("alias_before_publish", target, self.source)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"Done", result.stdout)
        self.unchanged(self.source)
        self.assertEqual(digest(target), self.initial[self.source])
        self.no_temporary_files()

    @unittest.skipUnless(os.name == "nt", "Windows DACL")
    def test_success_preserves_existing_dacl(self):
        subprocess.run(["icacls", str(self.output), "/inheritance:d"], check=True, capture_output=True)
        subprocess.run(["icacls", str(self.output), "/deny", "*S-1-5-32-546:(W)"], check=True, capture_output=True)
        before, flags = fs._permissions(self.output)
        result = self.driver("ply")
        self.assertEqual(result.returncode, 0, result.stderr)
        after, after_flags = fs._permissions(self.output)
        self.assertEqual(dacl_bytes(before), dacl_bytes(after))
        self.assertEqual(flags, after_flags)
        self.no_temporary_files()


@unittest.skipUnless(os.environ.get("PC2MESH_TEST_EXE"), "Set PC2MESH_TEST_EXE to the Release x64 executable")
class CliTests(TemporaryFiles):
    def cli(self, source, output):
        return subprocess.run([os.environ["PC2MESH_TEST_EXE"], "extract", str(source), str(output)],
                              cwd=self.root, capture_output=True, timeout=30)

    def test_iges_success_and_collisions(self):
        fixture = REPO / "tests" / "fixtures" / "five_points.igs"
        source = self.root / "space input.igs"
        shutil.copyfile(fixture, source)
        initial = digest(source)
        target = self.root / "space output.ply"
        for repetition in range(2):
            result = self.cli(source, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            lines = target.read_text().splitlines()
            self.assertIn("element vertex 5", lines)
            points = np.array([[float(x) for x in row.split()] for row in lines[lines.index("end_header") + 1:]])
            np.testing.assert_array_equal(points, [[i, 2 * i, 3 * i] for i in range(1, 6)])
        for alias in (source, self.alias(source, "hardlink")):
            result = self.cli(source, alias)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn(b"Done", result.stdout)
            self.assertEqual(digest(source), initial)
        self.no_temporary_files()

    def test_missing_input_and_busy_output_preserve_bytes(self):
        result = self.cli(self.root / "absent.igs", self.output)
        self.assertEqual(result.returncode, 1)
        self.unchanged(self.output)
        with no_delete_share(self.output):
            result = self.cli(REPO / "tests" / "fixtures" / "five_points.igs", self.output)
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(b"Done", result.stdout)
        self.unchanged(self.output)
        self.no_temporary_files()


class PythonCliTests(TemporaryFiles):
    def script(self, source, output, log):
        environment = os.environ.copy()
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.run([sys.executable, "-B", str(SCRIPTS / "reconstruct_poisson.py"),
                               str(source), str(output), str(log)], cwd=self.root, env=environment,
                              capture_output=True, timeout=60)

    def test_collisions_and_missing_input_exit_codes(self):
        for source, output, log in ((self.source, self.source, self.log),
                                    (self.source, self.output, self.source),
                                    (self.source, self.output, self.output)):
            with self.subTest(source=source, output=output, log=log):
                result = self.script(source, output, log)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn(b"Path conflict", result.stderr)
                self.assertNotIn(b"Status: OK", result.stdout)
                self.unchanged(self.source, self.output, self.log)
        result = self.script(self.root / "missing.ply", self.output, self.log)
        self.assertEqual(result.returncode, 1)
        self.unchanged(self.source, self.output)
        self.assertIn("Status: ERROR", self.log.read_text())
        self.no_temporary_files()

    def test_real_poisson_cli_with_spaces(self):
        index = np.arange(400)
        z = 1.0 - 2.0 * (index + 0.5) / len(index)
        angle = index * np.pi * (3.0 - np.sqrt(5.0))
        radius = np.sqrt(1.0 - z * z)
        points = np.column_stack((radius * np.cos(angle), radius * np.sin(angle), z))
        source = self.root / "sphere input.ply"
        self.assertTrue(o3d.io.write_point_cloud(str(source),
            o3d.geometry.PointCloud(o3d.utility.Vector3dVector(points)), write_ascii=True))
        before = digest(source)
        output = self.root / "mesh output.obj"
        log = self.root / "mesh log.txt"
        output.write_bytes(b"previous mesh")
        result = self.script(source, output, log)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(b"Status: OK", result.stdout)
        self.assertIn("Status: OK", log.read_text())
        self.assertEqual(digest(source), before)
        mesh = o3d.io.read_triangle_mesh(str(output))
        self.assertGreater(len(mesh.triangles), 0)
        self.assertTrue(np.isfinite(np.asarray(mesh.vertices)).all())
        self.no_temporary_files()


if __name__ == "__main__":
    unittest.main()
