"""腾讯云 COS 上传（HMAC-SHA1 签名，协议移植自官方 ima-skills cos-upload.cjs）。"""
from __future__ import annotations

import hashlib
import hmac
import http.client
import urllib.parse


def _hmac_sha1(key: str, data: str) -> str:
    return hmac.new(key.encode("utf-8"), data.encode("utf-8"), hashlib.sha1).hexdigest()


def _sha1(data: str) -> str:
    return hashlib.sha1(data.encode("utf-8")).hexdigest()


def build_authorization(secret_id: str, secret_key: str, method: str, pathname: str,
                        headers: dict, start_time: int, expired_time: int) -> str:
    key_time = f"{start_time};{expired_time}"
    sign_key = _hmac_sha1(secret_key, key_time)
    header_keys = sorted(headers.keys())
    http_headers = "&".join(f"{k.lower()}={urllib.parse.quote(str(headers[k]), safe='')}" for k in header_keys)
    http_string = f"{method.lower()}\n{pathname}\n\n{http_headers}\n"
    string_to_sign = f"sha1\n{key_time}\n{_sha1(http_string)}\n"
    signature = _hmac_sha1(sign_key, string_to_sign)
    header_list = ";".join(k.lower() for k in header_keys)
    return "&".join(
        [
            "q-sign-algorithm=sha1",
            f"q-ak={secret_id}",
            f"q-sign-time={key_time}",
            f"q-key-time={key_time}",
            f"q-header-list={header_list}",
            "q-url-param-list=",
            f"q-signature={signature}",
        ]
    )


def cos_upload(content: bytes, credential: dict, content_type: str, timeout: int = 120) -> None:
    """PUT Object 到 COS。credential 来自 IMA create_media 返回的 cos_credential。"""
    bucket = credential["bucket_name"]
    region = credential["region"]
    cos_key = credential["cos_key"]
    hostname = f"{bucket}.cos.{region}.myqcloud.com"
    pathname = f"/{cos_key}"
    sign_headers = {
        "content-length": str(len(content)),
        "host": hostname,
    }
    start = int(credential.get("start_time") or 0) or None
    expired = int(credential.get("expired_time") or 0) or None
    import time as _time

    now = int(_time.time())
    start_time = start or now - 60
    expired_time = expired or now + 3600
    authorization = build_authorization(
        credential["secret_id"], credential["secret_key"], "PUT", pathname, sign_headers, start_time, expired_time
    )
    conn = http.client.HTTPSConnection(hostname, timeout=timeout)
    try:
        conn.request(
            "PUT",
            pathname,
            body=content,
            headers={
                "Content-Type": content_type,
                "Content-Length": str(len(content)),
                "Authorization": authorization,
                "x-cos-security-token": credential["token"],
            },
        )
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", errors="ignore")
        if not (200 <= resp.status < 300):
            raise RuntimeError(f"COS 上传失败 HTTP {resp.status}: {body[:300]}")
    finally:
        conn.close()
