# D:/ContractReviewSys-合同审批智能审查项目/port_forward.py
"""
本地端口转发 —— 把 8080 的流量转到后端 8100

【为什么需要这个文件】
    cpolar 上那条名为 `website` 的公网隧道，其本地地址被固定在
    `http://localhost:8080`（见 ~/.cpolar/cpolar.yml）。而后端服务实际
    监听 8100（8000 端口被 vmnat.exe 占用，见项目长期记忆）。
    两者不一致时，公网访问会得到 502 —— 隧道本身是通的，只是"家门口
    没人接"。

    本脚本在 8080 上开一个纯 TCP 转发，把流量原样倒给 8100，
    于是既有的公网隧道无需改动任何 cpolar 配置即可直接访问本项目。

【与"直接改 cpolar 配置"的取舍】
    改 cpolar.yml 的 addr 为 8100 是更彻底的方案，但：
      1. 需要管理员权限重启 cpolar 服务（`net stop/start` 会拒绝访问）；
      2. 那条 website 隧道可能属于本机另一个项目（本机还有
         customer-service-mysql 容器），擅自改端口有副作用风险。
    因此选择在本机加一层转发，不改动用户的全局配置。

【用法】
    python port_forward.py                 # 8080 → 8100
    python port_forward.py --listen 8080 --target 8100
    python port_forward.py --listen 18080 --target 8100

【说明】
    纯 TCP 层转发，不解析 HTTP，因此不做任何请求/响应改写 ——
    Host 头、Cookie、multipart 上传、SSE 长连接全部原样透传。
    Ctrl+C 停止。
"""

import argparse
import asyncio
import sys
from pathlib import Path

# 让脚本在任意工作目录下都能被直接运行（读取项目内相对资源时不受影响）
sys.path.insert(0, str(Path(__file__).resolve().parent))


# 单次数据块大小：64 KiB 是 asyncio 场景下吞吐与内存占用的常用折中值，
# 足够覆盖合同 PDF 上传（通常数 MB）而不至于让单连接占用过多缓冲。
CHUNK_SIZE = 64 * 1024


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """
    单向搬运：把 reader 的数据持续写入 writer，直到对端关闭。

    参数：
        reader: 数据来源
        writer: 数据去向

    说明：
        采用"读一块写一块 + drain 背压"的写法，保证慢速客户端不会
        把内存撑爆（相比一次性 read() 全量读取安全得多）。
    """
    try:
        while True:
            chunk = await reader.read(CHUNK_SIZE)
            if not chunk:
                # 对端正常关闭（EOF）
                break
            writer.write(chunk)
            # 等待缓冲区排空，形成背压；否则大文件传输会堆积内存
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        # 浏览器中途取消请求、或目标服务主动断开等，属正常现象，静默收尾
        pass
    finally:
        # 关闭写端，向对端发送 FIN；重复 close 会抛错，故用 suppress 兜住
        try:
            writer.close()
        except Exception:
            pass


async def _handle_client(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
) -> None:
    """
    处理单个入站连接：与目标服务建立连接后开启双向搬运。

    参数：
        client_reader / client_writer: 入站连接的两端
        target_host / target_port:     后端服务地址
    """
    peer = client_writer.get_extra_info("peername")
    try:
        target_reader, target_writer = await asyncio.open_connection(
            target_host, target_port
        )
    except OSError as exc:
        # 后端没起来时最常走到这里：明确告诉调用方，避免排查时误以为是隧道问题
        print(f"[转发] 目标不可达 {target_host}:{target_port} ← {peer} ({exc})", flush=True)
        try:
            # 返回 502，与"隧道通了但后端没接"的语义保持一致，便于排查
            client_writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                b"Content-Length: 33\r\n"
                b"Connection: close\r\n\r\n"
                b"502 Bad Gateway: backend offline"
            )
            await client_writer.drain()
        except Exception:
            pass
        finally:
            try:
                client_writer.close()
            except Exception:
                pass
        return

    # 双向并发搬运；任一侧结束即整体收尾（HTTP 场景下足够）
    await asyncio.gather(
        _pipe(client_reader, target_writer),
        _pipe(target_reader, client_writer),
        return_exceptions=True,
    )


async def main(listen_port: int, target_host: str, target_port: int) -> None:
    """
    启动转发服务并常驻运行。

    参数：
        listen_port:  本地监听端口（公网隧道指向它）
        target_host:  后端主机
        target_port:  后端端口
    """
    server = await asyncio.start_server(
        lambda r, w: _handle_client(r, w, target_host, target_port),
        host="0.0.0.0",  # 必须绑 0.0.0.0：cpolar 从本机其它网卡接入
        port=listen_port,
    )

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    print("=" * 62, flush=True)
    print("本地端口转发已启动", flush=True)
    print(f"  监听   : {addrs}", flush=True)
    print(f"  目标   : {target_host}:{target_port}", flush=True)
    print(f"  用途   : cpolar website 隧道 (localhost:{listen_port}) → 后端", flush=True)
    print("  停止   : Ctrl+C", flush=True)
    print("=" * 62, flush=True)

    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="本地端口转发（默认 8080 → 8100）")
    parser.add_argument("--listen", type=int, default=8080, help="本地监听端口")
    parser.add_argument("--target-host", default="127.0.0.1", help="后端主机")
    parser.add_argument("--target", type=int, default=8100, help="后端端口")
    args = parser.parse_args()

    try:
        asyncio.run(main(args.listen, args.target_host, args.target))
    except KeyboardInterrupt:
        print("\n[转发] 已停止", flush=True)
