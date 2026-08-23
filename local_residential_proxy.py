# -*- coding: utf-8 -*-
"""本地无认证 HTTP 代理 → 住宅代理(SOCKS5+认证)转发器

链路: Chrome → 127.0.0.1:18080(本脚本) → v2rayN socks5:10808 → VPS → 住宅代理 104.140.99.69:36270 → 目标站

用途: Chrome --proxy-server 不支持用户名密码认证,此脚本将住宅代理的
SOCKS5 认证转发为本地无认证 HTTP 代理。

用法: py local_residential_proxy.py
"""
import asyncio
import socket
import struct
import sys

LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18080

V2RAY_SOCKS5 = ("127.0.0.1", 10808)
RESI_SOCKS5 = ("104.140.99.69", 36270)
RESI_USER = "22A3ZFDA1041409969A36270"
RESI_PASS = "eWqfIBvgs1Nt"


async def socks5_connect(reader, writer, host: str, port: int, user=None, password=None) -> None:
    """建立 SOCKS5 隧道;host 可为 IPv4 或域名(0x03 域名模式由上游解析)"""
    writer.write(b"\x05\x01\x00")
    await writer.drain()
    resp = await reader.readexactly(2)
    if resp[0] != 0x05:
        raise ConnectionError(f"上游非 SOCKS5: {resp}")
    method = resp[1]
    if method == 0x02 and user is not None:
        ub = user.encode()
        pb = password.encode()
        writer.write(b"\x01" + bytes([len(ub)]) + ub + bytes([len(pb)]) + pb)
        await writer.drain()
        auth = await reader.readexactly(2)
        if auth != b"\x01\x00":
            raise ConnectionError(f"SOCKS5 认证失败: {auth}")
    elif method != 0x00:
        raise ConnectionError(f"上游要求不支持的认证方式: {method}")
    try:
        req = b"\x05\x01\x00\x01" + socket.inet_aton(host) + struct.pack(">H", port)
    except OSError:
        hb = host.encode()
        req = b"\x05\x01\x00\x03" + bytes([len(hb)]) + hb + struct.pack(">H", port)
    writer.write(req)
    await writer.drain()
    head = await reader.readexactly(4)
    if head[1] != 0x00:
        raise ConnectionError(f"SOCKS5 CONNECT 失败: code={head[1]}")
    atyp = head[3]
    if atyp == 0x01:
        await reader.readexactly(4)
    elif atyp == 0x03:
        ln = (await reader.readexactly(1))[0]
        await reader.readexactly(ln)
    elif atyp == 0x04:
        await reader.readexactly(16)
    await reader.readexactly(2)


async def build_chain(host: str, port: int):
    """建立 本机→v2ray→住宅代理 的 SOCKS5 链,返回 (reader, writer)

    在经 v2ray 到达住宅代理的同一条连接上完成 SOCKS5 认证并 CONNECT 目标。
    """
    r1, w1 = await asyncio.open_connection(*V2RAY_SOCKS5)
    try:
        await socks5_connect(r1, w1, RESI_SOCKS5[0], RESI_SOCKS5[1])
        await socks5_connect(r1, w1, host, port, RESI_USER, RESI_PASS)
        return r1, w1
    except Exception:
        w1.close()
        raise


async def pipe(src, dst):
    try:
        while True:
            data = await src.read(65536)
            if not data:
                break
            dst.write(data)
            await dst.drain()
    except (ConnectionError, asyncio.TimeoutError):
        pass
    finally:
        try:
            dst.close()
        except Exception:
            pass


async def handle_client(cli_reader, cli_writer):
    peer = cli_writer.get_extra_info("peername")
    try:
        # 读请求行
        line = await cli_reader.readline()
        if not line:
            return
        parts = line.decode("latin1").strip().split(" ")
        method, target = parts[0], parts[1]
        # 消费剩余请求头
        while True:
            h = await cli_reader.readline()
            if h in (b"\r\n", b"\n", b""):
                break

        if method == "CONNECT":
            host, _, port_s = target.partition(":")
            port = int(port_s)
            r, w = await build_chain(host, port)
            cli_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await cli_writer.drain()
            await asyncio.gather(
                pipe(cli_reader, w),
                pipe(r, cli_writer),
                return_exceptions=True,
            )
        else:
            # 明文 HTTP: 解析绝对 URL, CONNECT 后重写请求行
            from urllib.parse import urlsplit
            u = urlsplit(target)
            host = u.hostname
            port = u.port or 80
            r, w = await build_chain(host, port)
            path = u.path or "/"
            if u.query:
                path += "?" + u.query
            req = f"{method} {path} HTTP/1.1\r\nHost: {host}\r\n"
            # 透传剩余请求体(简单场景无 body)
            w.write(req.encode("latin1"))
            await w.drain()
            await asyncio.gather(
                pipe(cli_reader, w),
                pipe(r, cli_writer),
                return_exceptions=True,
            )
    except Exception as e:
        print(f"[{peer}] 错误: {e}")
    finally:
        try:
            cli_writer.close()
        except Exception:
            pass


async def main():
    server = await asyncio.start_server(handle_client, LISTEN_HOST, LISTEN_PORT)
    print(f"转发代理已启动: http://{LISTEN_HOST}:{LISTEN_PORT}")
    print(f"链路: 本机 → v2rayN:{V2RAY_SOCKS5[1]} → {RESI_SOCKS5[0]}:{RESI_SOCKS5[1]} → 目标")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
