"""Check that this machine matches the team setup. Run: .venv\\Scripts\\python.exe verify_setup.py

By default it only connects to the Bloomberg Terminal (no data is downloaded, so it does not
use any of the daily API allowance). Add --data to also download one price as a final test.
"""
import importlib
import importlib.metadata
import re
import socket
import sys
from pathlib import Path

EXPECTED_PYTHON = (3, 13)
REQUIREMENT_FILES = ["requirements.txt", "requirements-bloomberg.txt"]
results = []


def pinned_packages(root):
    """name -> version for every 'name==version' line of the requirement files."""
    pins = {}
    for name in REQUIREMENT_FILES:
        for line in (root / name).read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*([A-Za-z0-9_.-]+)==([^\s#]+)", line)
            if m:
                pins[m.group(1)] = m.group(2)
    return pins


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'OK' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def main():
    root = Path(__file__).resolve().parent
    exe = Path(sys.executable)
    check("Python version", sys.version_info[:2] == EXPECTED_PYTHON,
          f"{sys.version.split()[0]} (expected {EXPECTED_PYTHON[0]}.{EXPECTED_PYTHON[1]}.x)")
    check("64-bit Python", sys.maxsize > 2**32)
    check("Running from the project .venv", (root / ".venv") in exe.parents, str(exe))

    for pkg, wanted in pinned_packages(root).items():
        try:
            have = importlib.metadata.version(pkg)
            check(f"package {pkg}", have == wanted, have if have == wanted else f"{have} installed, {wanted} expected")
        except importlib.metadata.PackageNotFoundError:
            check(f"package {pkg}", False, f"not installed ({wanted} expected)")
    try:
        importlib.import_module("blpapi")
        check("import blpapi", True)
    except Exception as e:
        check("import blpapi", False, str(e))

    with socket.socket() as s:
        s.settimeout(3)
        port_open = s.connect_ex(("127.0.0.1", 8194)) == 0
    check("Bloomberg API port 8194 (bbcomm)", port_open,
          "" if port_open else "start the Bloomberg Terminal and log in")
    if not port_open:
        return

    import blpapi
    opts = blpapi.SessionOptions()
    opts.setServerHost("localhost")
    opts.setServerPort(8194)
    session = blpapi.Session(opts)
    started = session.start()
    check("Bloomberg session start", started)
    if not started:
        return
    check("Service //blp/refdata", session.openService("//blp/refdata"))

    if "--data" in sys.argv:
        svc = session.getService("//blp/refdata")
        req = svc.createRequest("ReferenceDataRequest")
        req.append("securities", "SPX Index")
        req.append("fields", "PX_LAST")
        session.sendRequest(req)
        price = None
        while True:
            ev = session.nextEvent(10000)
            for msg in ev:
                if msg.hasElement("securityData"):
                    fd = msg.getElement("securityData").getValueAsElement(0).getElement("fieldData")
                    if fd.hasElement("PX_LAST"):
                        price = fd.getElementAsFloat("PX_LAST")
            if ev.eventType() == blpapi.Event.RESPONSE:
                break
        check("Download test (SPX Index PX_LAST)", price is not None, str(price))
    session.stop()


if __name__ == "__main__":
    main()
    print("\nALL CHECKS PASSED" if all(results) else "\nSOME CHECKS FAILED - see the troubleshooting section of the guide")
    sys.exit(0 if all(results) else 1)
