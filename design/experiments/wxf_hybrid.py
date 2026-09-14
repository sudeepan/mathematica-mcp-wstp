"""Can wolframclient's WXF decoder consume bytes pulled off a WSTP link?"""
import ctypes, sys, time
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
import wstp_client as W
from wstp_client import K, RETURNPKT
w = W.w
w.WSGetByteString.argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
                            ctypes.POINTER(ctypes.c_int), ctypes.c_int]
w.WSGetByteString.restype=ctypes.c_int
w.WSReleaseByteString.argtypes=[ctypes.c_void_p, ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
w.WSReleaseByteString.restype=None

k = K()
def raw_bytes(expr, timeout=60):
    """Evaluate expr and pull back Normal[BinarySerialize[expr]] as raw bytes."""
    w.WSPutFunction(k.l, b"EvaluatePacket", 1)
    w.WSPutFunction(k.l, b"Normal", 1)
    w.WSPutFunction(k.l, b"BinarySerialize", 1)
    w.WSPutFunction(k.l, b"ToExpression", 1)
    w.WSPutString(k.l, expr.encode())
    w.WSEndPacket(k.l); w.WSFlush(k.l)
    t0=time.time()
    while time.time()-t0 < timeout:
        if w.WSReady(k.l):
            if w.WSNextPacket(k.l) == RETURNPKT:
                arr = ctypes.POINTER(ctypes.c_ubyte)(); n = ctypes.c_int(0)
                if w.WSGetInteger8List(k.l, ctypes.byref(arr), ctypes.byref(n)):
                    data = bytes(bytearray(arr[i] & 0xFF for i in range(n.value)))
                    w.WSNewPacket(k.l); return data
            w.WSNewPacket(k.l)
        time.sleep(0.0005)
    return None

EXPR = '{1, 2.5, "abc", Sin[x], <|"k" -> {1, 2, 3}|>, 1/3}'
data = raw_bytes(EXPR)
print(f"WXF bytes off the WSTP link: {len(data)} bytes, magic={data[:2]!r}")

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src"))
from wolframclient.deserializers import binary_deserialize
out = binary_deserialize(data)
print("wolframclient decoded ->", out)
print("types ->", [type(o).__name__ for o in out])
k.close()
