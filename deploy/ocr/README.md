# OCR 独立 Docker 部署

直接复用 `module/ocr/rpc.py` 与 `module/ocr/ppocr.py`，只附带异常和日志模块。
容器不需要 FastAPI、模拟器、账号配置或游戏任务代码。

## 启动

在项目根目录或仅包含以下文件的部署目录构建：

- `module/exception.py`
- `module/logger.py`
- `module/ocr/rpc.py`
- `module/ocr/ppocr.py`
- `deploy/ocr/`

创建 `deploy/ocr/.env`，设置服务器的可信内网地址：

```dotenv
OCR_BIND_ADDRESS=192.168.50.164
OCR_WORKERS=4
```

```bash
docker compose -f deploy/ocr/compose.yaml up -d --build --wait --wait-timeout 120
docker compose -f deploy/ocr/compose.yaml ps
docker compose -f deploy/ocr/compose.yaml logs --tail=100 ocr
```

不设置绑定地址时仅监听宿主机 `127.0.0.1`。RPC 使用 pickle，无鉴权或 TLS，
只允许可信网络访问，不要映射到公网或公共隧道。绑定内网地址本身不等于客户端鉴权。

Compose 默认 4 个 OCR 工作线程，CPU 推理；容器限制 4 个 CPU 配额、3 GiB 内存。
线程按需加载模型。健康检查只验证 RPC 响应，实际识别仍需执行下面的冒烟测试。
`unless-stopped` 在进程退出或 Docker 重启后自动启动；手动停止后保持停止，
健康状态异常本身不会触发 Docker 自动重启。
日志可通过 `docker compose logs` 查看；容器文件日志位于临时内存目录。

## 客户端验证与切换

在 Windows OAS 工程根目录，使用现有客户端验证远程单行识别和文本框检测：

```powershell
.\toolkit\python.exe -m deploy.ocr.smoke_test 192.168.50.164:22268
.\toolkit\python.exe -m deploy.ocr.smoke_test 192.168.50.164:22268 --image tasks/Component/SwitchSoul/ss/ss_sou_switch_sure.png --roi 52 10 130 50 --expected 确定
```

第一条使用生成的 `12345` 图片，第二条裁剪工程自带游戏按钮中的文字区域。
检测接口沿用游戏规则的正方形补边方式，单行接口只接收文字 ROI，避免按钮边框被识别为标点。
两种 RPC 识别方法都必须返回预期文字，否则以非零状态退出。

切换正式任务时，修改已有 `config/deploy.yaml` 中的 `Deploy.Ocr`：

```yaml
StartOcrServer: false
OcrClientAddress: 192.168.50.164:22268
```

重启本地后端与脚本进程后生效。客户端保留原来的 10 秒超时，远端不可用时不会自动回退本机。
`StartOcrServer: false` 时，后端启动检查连接失败只记警告，随后继续启动 HTTP 服务；
任务首次使用 OCR 时会自行连接，仍不可用则向任务抛出异常。启动检查仍可能等待连接超时。
`StartOcrServer: true` 时继续要求本机 OCR 就绪后才启动后端。
本目录的部署操作不会自动改写本地账号或部署配置。

## 运维与更新

本次部署目标为 `ubuntu-leovefy:/home/leovefy/oas-ocr`，容器名 `oas-ocr`。

```bash
cd /home/leovefy/oas-ocr
docker compose -f deploy/ocr/compose.yaml restart ocr
docker compose -f deploy/ocr/compose.yaml stop ocr
docker compose -f deploy/ocr/compose.yaml up -d --wait
```

更新时同步上述最小文件集，再执行 `up -d --build --wait`。
`requirements-in.txt` 为直接依赖，`requirements.txt` 为 OCR 所需依赖集合，版本对齐仓库根依赖文件。
更换依赖版本时同时核对两份清单，构建中运行 `pip check` 验证依赖兼容。
镜像内使用独立虚拟环境和 `wheel==0.41.3`，避免基础镜像的新构建工具与项目
固定的 `packaging==20.9` 冲突。

恢复本机 OCR 时将 `StartOcrServer` 改回 `true`、`OcrClientAddress` 改回
`127.0.0.1:22268`，重启本地后端与脚本；再按需停止远程容器。
