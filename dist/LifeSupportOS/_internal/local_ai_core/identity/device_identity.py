"""
identity/device_identity.py
----------------------------
ログイン不要の端末内ID発行と、パスフレーズからの鍵導出・暗号化/復号を行う。

Archlife `frontend-integration/cryptoStorage.js` の設計
(PBKDF2でパスフレーズから鍵導出 → AES-GCMで暗号化 → サーバーは中身を見ない)を、
サーバー/デスクトップアプリ側でも同じ方式で使えるようPythonに移植したもの。

依存: `cryptography` パッケージ (pip install cryptography)
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

_PBKDF2_ITERATIONS = 150_000  # cryptoStorage.js と同じ反復回数
_KEY_LENGTH_BYTES = 32  # AES-256
_IV_LENGTH_BYTES = 12  # AES-GCM推奨IV長


@dataclass
class EncryptedBlob:
    ciphertext_b64: str
    iv_b64: str

    def to_dict(self) -> dict:
        return {"ciphertext": self.ciphertext_b64, "iv": self.iv_b64}

    @classmethod
    def from_dict(cls, d: dict) -> "EncryptedBlob":
        return cls(ciphertext_b64=d["ciphertext"], iv_b64=d["iv"])


class DeviceIdentity:
    """端末ごとに1つ発行される匿名ID + 鍵導出用salt を管理する。

    Archlifeの `getOrCreateAnonId()`(localStorageにUUIDを保存)を、
    サーバー/デスクトップアプリ側のファイル保存に置き換えたもの。
    """

    def __init__(self, storage_path: str = "device_identity.json"):
        self.storage_path = Path(storage_path)
        self._data = self._load_or_create()

    def _load_or_create(self) -> dict:
        if self.storage_path.exists():
            return json.loads(self.storage_path.read_text(encoding="utf-8"))
        data = {
            "device_id": str(uuid.uuid4()),
            "key_salt": base64.b64encode(os.urandom(16)).decode("ascii"),
        }
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_path.write_text(json.dumps(data), encoding="utf-8")
        return data

    @property
    def device_id(self) -> str:
        return self._data["device_id"]

    @property
    def key_salt(self) -> bytes:
        return base64.b64decode(self._data["key_salt"])

    def derive_key(self, passphrase: str) -> bytes:
        """cryptoStorage.js の deriveKey() と同一パラメータ(PBKDF2-SHA256, 150000回)。
        パスフレーズを忘れると復号できなくなる設計上の制約も同様に引き継ぐ。
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=_KEY_LENGTH_BYTES,
            salt=self.key_salt,
            iterations=_PBKDF2_ITERATIONS,
        )
        return kdf.derive(passphrase.encode("utf-8"))

    def encrypt_json(self, key: bytes, obj: dict) -> EncryptedBlob:
        iv = os.urandom(_IV_LENGTH_BYTES)
        aesgcm = AESGCM(key)
        plaintext = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        ciphertext = aesgcm.encrypt(iv, plaintext, None)
        return EncryptedBlob(
            ciphertext_b64=base64.b64encode(ciphertext).decode("ascii"),
            iv_b64=base64.b64encode(iv).decode("ascii"),
        )

    def decrypt_json(self, key: bytes, blob: EncryptedBlob) -> dict:
        aesgcm = AESGCM(key)
        iv = base64.b64decode(blob.iv_b64)
        ciphertext = base64.b64decode(blob.ciphertext_b64)
        plaintext = aesgcm.decrypt(iv, ciphertext, None)
        return json.loads(plaintext.decode("utf-8"))
