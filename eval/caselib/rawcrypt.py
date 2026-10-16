"""Hand-assembled encryption for rawpdf: the PDF standard security
handler, RC4 only (revisions 2-4: 40- and 128-bit; revision 4 through a
/CF crypt filter, /CFM /V2), with the empty
user password -- the owner-password-only file a redaction pipeline
produces. Stdlib only, so the ledger's tests can build an encrypted file
without pikepdf, and so a case can encrypt one object with a key the
file's /U does not authenticate, or flip a byte of its ciphertext: what
no conforming writer emits.

AES (128-bit R4, 256-bit R6) needs a block cipher the stdlib lacks; the
case library builds those with pikepdf (families/encrypted.py).
"""

from __future__ import annotations

import hashlib
import re
import struct
from dataclasses import dataclass

# The spec's 32-byte password padding (ISO 32000-1, 7.6.3.3, Algorithm 2).
PAD = bytes.fromhex("28bf4e5e4e758a4164004e56fffa01082e2e00b6d0683e802f0ca9fe6453697a")


def rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray()
    i = j = 0
    for byte in data:
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out.append(byte ^ s[(s[i] + s[j]) & 0xFF])
    return bytes(out)


def _padded(password: bytes) -> bytes:
    return (password + PAD)[:32]


@dataclass(frozen=True)
class Handler:
    """A standard security handler's /Encrypt values and file key.

    *revision* 2 is 40-bit RC4, 3 is 128-bit, 4 is 128-bit through a
    crypt filter named StdCF (*stm_f* and *str_f* name the defaults for
    streams and strings: "StdCF" or "Identity"; *encrypt_metadata*
    False changes the file key, and the caller then writes the catalog's
    /Metadata stream unencrypted); *owner* is the owner password; the user
    password is empty. *permissions* is /P.
    """

    revision: int
    owner: bytes
    file_id: bytes
    permissions: int = -3904        # every permission withheld (bits 3-6, 9-12 clear)
    stm_f: str = "StdCF"
    str_f: str = "StdCF"
    encrypt_metadata: bool = True

    def __post_init__(self) -> None:
        if self.revision not in (2, 3, 4):
            raise ValueError("rawcrypt writes RC4 revisions 2-4 only")
        if self.revision != 4 and (self.stm_f, self.str_f, self.encrypt_metadata) != (
                "StdCF", "StdCF", True):
            raise ValueError("crypt filters and EncryptMetadata need revision 4")

    @property
    def length(self) -> int:
        return 5 if self.revision == 2 else 16

    @property
    def o(self) -> bytes:
        """/O (Algorithm 3): the padded user password under a key from
        the owner password."""
        digest = hashlib.md5(_padded(self.owner)).digest()
        if self.revision >= 3:
            for _ in range(50):
                digest = hashlib.md5(digest[:self.length]).digest()
        key = digest[:self.length]
        out = rc4(key, _padded(b""))
        if self.revision >= 3:
            for i in range(1, 20):
                out = rc4(bytes(b ^ i for b in key), out)
        return out

    @property
    def key(self) -> bytes:
        """The file key (Algorithm 2) for the empty user password."""
        metadata = b"" if self.encrypt_metadata else b"\xff\xff\xff\xff"
        digest = hashlib.md5(_padded(b"") + self.o + struct.pack("<i", self.permissions)
                             + self.file_id + metadata).digest()
        if self.revision >= 3:
            for _ in range(50):
                digest = hashlib.md5(digest[:self.length]).digest()
        return digest[:self.length]

    @property
    def u(self) -> bytes:
        """/U (Algorithms 4 and 5): what authenticates the empty user
        password."""
        if self.revision == 2:
            return rc4(self.key, PAD)
        out = rc4(self.key, hashlib.md5(PAD + self.file_id).digest())
        for i in range(1, 20):
            out = rc4(bytes(b ^ i for b in self.key), out)
        return out + bytes(16)

    def encrypt_dict(self) -> bytes:
        v = {2: 1, 3: 2, 4: 4}[self.revision]
        bits = b"" if self.revision == 2 else b" /Length 128"
        if self.revision == 4:
            bits += (b" /CF << /StdCF << /CFM /V2 /Length 16 /AuthEvent /DocOpen >> >>"
                     b" /StmF /%s /StrF /%s" % (self.stm_f.encode(), self.str_f.encode()))
            if not self.encrypt_metadata:
                bits += b" /EncryptMetadata false"
        return (b"<< /Filter /Standard /V %d /R %d%s /O <%s> /U <%s> /P %d >>"
                % (v, self.revision, bits, self.o.hex().encode(), self.u.hex().encode(),
                   self.permissions))

    def trailer(self, encrypt: int) -> bytes:
        """The trailer entries: /Encrypt (object *encrypt*) and /ID."""
        return b" /Encrypt %d 0 R /ID [<%s> <%s>]" % (
            encrypt, self.file_id.hex().encode(), self.file_id.hex().encode())

    def object_key(self, number: int, gen: int = 0, *, key: bytes | None = None) -> bytes:
        """Algorithm 1: the key for one object's strings and streams;
        *key* replaces the file key (a case's wrong key)."""
        base = self.key if key is None else key
        digest = hashlib.md5(base + number.to_bytes(4, "little")[:3]
                             + gen.to_bytes(4, "little")[:2]).digest()
        return digest[:min(len(base) + 5, 16)]

    def string(self, number: int, text: bytes, gen: int = 0, *,
               key: bytes | None = None) -> bytes:
        """*text* encrypted for object *number*, as a hex string token."""
        return b"<%s>" % rc4(self.object_key(number, gen, key=key), text).hex().encode()

    def stream_data(self, number: int, data: bytes, gen: int = 0, *,
                    key: bytes | None = None) -> bytes:
        return rc4(self.object_key(number, gen, key=key), data)


def flip_last_block(data: bytes, position: int, xor: int) -> bytes:
    """AES-CBC stream or string *data* (16-byte IV, then whole blocks)
    with byte *position* (0-15) of its last plaintext block XORed with
    *xor*: the same byte of the ciphertext block before it (the IV, for
    one block) is flipped, and no key is needed. Turning a valid padding
    ``02 02`` into ``07 02`` makes one that is inconsistent but whose
    last byte is in range: pikepdf keeps it, MuPDF strips it."""
    if len(data) < 32 or len(data) % 16 or not 0 <= position < 16:
        raise ValueError("need an IV and whole 16-byte blocks, and a position 0-15")
    at = len(data) - 32 + position
    return data[:at] + bytes([data[at] ^ xor]) + data[at + 1:]


def stream_span(pdf: bytes, number: int) -> tuple[int, int]:
    """Where object *number*'s stream data lies in *pdf*: the object must
    be written ``N 0 obj`` once (generation 0), its dictionary closed by
    ``>>`` right before ``stream`` and its end of line, with a direct
    /Length -- the first /Length in the object, so a nested one
    (/DecodeParms) must not come first."""
    head = b"\n%d 0 obj" % number
    if pdf.count(head) != 1:
        raise ValueError(f"object {number} is not written once as `{number} 0 obj`")
    at = pdf.index(head)
    opening = re.compile(rb">>\s*stream(\r\n|\n)").search(pdf, at)
    if opening is None:
        raise ValueError(f"object {number} is not a stream")
    length = re.search(rb"/Length (\d+)\s*(?:/|>>)", pdf[at:opening.start() + 2])
    if length is None:
        raise ValueError(f"object {number} has no direct /Length")
    return opening.end(), opening.end() + int(length.group(1))


def replace_stream_data(pdf: bytes, number: int, data: bytes) -> bytes:
    """*pdf* with object *number*'s stream data (``stream_span``) replaced
    by *data* of the same length, so every offset still holds."""
    start, end = stream_span(pdf, number)
    if end - start != len(data):
        raise ValueError("the new data is not the stream's /Length")
    return pdf[:start] + data + pdf[end:]
