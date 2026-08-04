"""Transport between the application (Pi side) and the motion controller.

Two implementations of the same interface:
  SerialTransport - the real ESP32-S3 over USB serial (use on the Pi, or on
                    the PC once you have the board)
  SimTransport    - in-process simulated ESP32 firmware (sorter/esp32_sim.py)

The app only ever calls send() / readline(), so swapping hardware in later
is a command-line flag, not a code change.
"""
import queue


class Transport:
    def send(self, line: str):
        raise NotImplementedError

    def readline(self, timeout: float = None) -> str | None:
        """Next line from the controller, or None on timeout."""
        raise NotImplementedError

    def close(self):
        pass


class SerialTransport(Transport):
    def __init__(self, port, baud=115200):
        import serial  # pyserial; imported lazily so sim mode needs no serial port
        self.ser = serial.Serial(port, baud, timeout=0.1)

    def send(self, line):
        self.ser.write((line + "\n").encode())

    def readline(self, timeout=None):
        import time
        deadline = None if timeout is None else time.monotonic() + timeout
        buf = b""
        while True:
            chunk = self.ser.readline()  # returns b"" after 0.1 s of silence
            buf += chunk
            if buf.endswith(b"\n"):
                return buf.decode(errors="replace").strip()
            if deadline is not None and time.monotonic() >= deadline:
                return None

    def close(self):
        self.ser.close()


class SimTransport(Transport):
    """Wraps Esp32Sim; lines flow through queues instead of a UART."""

    def __init__(self, sim):
        self.sim = sim
        sim.attach(self)
        self.to_app = queue.Queue()

    def send(self, line):
        self.sim.handle_line(line.strip())

    def readline(self, timeout=None):
        try:
            return self.to_app.get(timeout=timeout)
        except queue.Empty:
            return None

    # called by the sim ("firmware -> Pi" direction)
    def emit(self, line):
        self.to_app.put(line)

    def close(self):
        self.sim.stop()
