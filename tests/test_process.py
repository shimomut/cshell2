"""Tests for the process multiplexing module."""

import os
import time

from cshell2.process import OutputBuffer, ProcessSlot


class TestOutputBuffer:
    def test_append_and_drain(self):
        buf = OutputBuffer()
        buf.append(b"hello")
        buf.append(b"world")
        chunks = buf.drain()
        assert chunks == [b"hello", b"world"]

    def test_drain_clears(self):
        buf = OutputBuffer()
        buf.append(b"data")
        buf.drain()
        assert buf.drain() == []

    def test_max_chunks(self):
        buf = OutputBuffer(max_chunks=3)
        for i in range(5):
            buf.append(f"chunk{i}".encode())
        chunks = buf.drain()
        assert len(chunks) == 3
        assert chunks == [b"chunk2", b"chunk3", b"chunk4"]


class TestProcessSlot:
    def test_start_and_exit(self):
        slot = ProcessSlot()
        slot.start(argv=["echo", "hello"], env=dict(os.environ), cwd=os.getcwd())
        slot.activate()
        slot.wait(timeout=5)
        assert not slot.is_alive()
        assert slot.exit_code == 0

    def test_output_buffered_when_inactive(self):
        slot = ProcessSlot()
        slot.start(argv=["echo", "buffered-output"], env=dict(os.environ), cwd=os.getcwd())
        # Don't activate — output goes to buffer only
        slot.wait(timeout=5)
        chunks = slot.buffer.drain()
        combined = b"".join(chunks)
        assert b"buffered-output" in combined

    def test_write_stdin(self):
        slot = ProcessSlot()
        slot.start(
            argv=["cat"],
            env=dict(os.environ),
            cwd=os.getcwd(),
        )
        slot.activate()
        slot.write_stdin(b"test-input\n")
        # Send EOF to cat
        slot.write_stdin(b"\x04")
        slot.wait(timeout=5)
        assert slot.exit_code == 0
        chunks = slot.buffer.drain()
        combined = b"".join(chunks)
        assert b"test-input" in combined

    def test_kill(self):
        slot = ProcessSlot()
        slot.start(argv=["sleep", "60"], env=dict(os.environ), cwd=os.getcwd())
        assert slot.is_alive()
        slot.kill()
        slot.wait(timeout=5)
        assert not slot.is_alive()

    def test_exit_code_nonzero(self):
        slot = ProcessSlot()
        slot.start(argv=["false"], env=dict(os.environ), cwd=os.getcwd())
        slot.wait(timeout=5)
        assert slot.exit_code == 1
