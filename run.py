"""NovelFlow 开发入口。

用法：
    python run.py

然后打开浏览器访问 http://localhost:8021
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv()
    os.environ.setdefault("LOG_LEVEL", "INFO")

    from src.main import app

    host = os.getenv("WEB_HOST", "127.0.0.1")
    port = int(os.getenv("WEB_PORT", "8021"))

    print()
    print("  ✍️  NovelFlow - AI 小说全流程创作工作台")
    print(f"  http://localhost:{port}")
    print(f"  API 文档: http://localhost:{port}/docs")
    print()

    uvicorn.run(
        app,
        host=host,
        port=port,
        reload=False,
        log_level="info",
    )
