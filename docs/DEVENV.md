# 开发环境注意事项（DEVENV）

## 关键约束：DSH 会话内无法加载 PyPI 原生二进制

在 DSH（DeepSeek Harness）bash 工具进程链下启动的 python，进程带有
`CS_HARD|CS_KILL|CS_ENFORCEMENT` 强制 library validation（csops flags=0x22011311），
AMFI 拒绝加载任何非同 Team 的二进制，错误形如：

```
OSError: dlopen(.../numpy/_core/_multiarray_umath.cpython-312-darwin.so, ...):
code signature in <UUID> ... not valid for use in process:
mapping process and mapped file (non-platform) have different Team IDs
```

受影响（DSH 内 import 必失败）：numpy、cv2、scipy、pydantic_core、rawpy、
pillow_heif、PIL._imaging、imagehash、fastapi（→pydantic_core）。
不受影响（纯 Python / DSH 签名）：sqlite3、pathlib、shutil、pytest、pluggy、
httpx 的非 TestClient 用法；DSH runtime 自带已签名的 Pillow/numpy 可用但版本另算。

**结论：用户自己的终端不受影响**（.venv/bin/python3.12 已是 ad-hoc 签名副本，
用户终端中 adhoc 进程加载 adhoc 库正常）。因此采用双轨验证：

1. **DSH 内**：跑纯 Python 测试（db/config/scanner 文件与配对逻辑/recommender 理由/
   executor 移动撤销/learner 纯 Python 优化）。依赖原生库的测试必须加 skip 保护：
   模块顶部 `pytest.importorskip("cv2")` 或 `pytest.mark.skipif(os.environ.get("PHOTO_PICKER_SKIP_NATIVE"), ...)`
2. **用户终端**：跑全量 `pytest` 与 `./run.sh`（uvicorn 127.0.0.1:8765）。
3. **联调**：用户终端起服务后，DSH 内用 `curl http://127.0.0.1:8765/api/...` 做端到端
   API 验证（curl 独立进程不受影响）。

## 代码风格要求（由约束推导）

- 原生库 import 一律**延迟到函数内部**（`def analyze(): import cv2`），保证模块 import
  纯净、路由注册与纯逻辑可测。
- learner 用手写梯度下降（不用 scipy.optimize），保持纯 Python 可测。
- 测试文件按是否触碰原生库拆分，触碰的文件整体 importorskip。

## 运行命令

```bash
# DSH 内 / 用户终端通用（纯 Python 部分）
.venv/bin/python -m pytest backend/tests -v

# 用户终端全量（含原生库部分）
cd backend && ../.venv/bin/python -m pytest tests -v
./run.sh   # 构建前端 + 启动 uvicorn + 打开浏览器
```

python: `.venv/bin/python`（3.12.14，基于 DSH runtime；pyvenv.cfg 指向 DSH python home）
