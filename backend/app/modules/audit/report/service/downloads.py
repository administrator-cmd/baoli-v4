"""文本报告的下载响应；报告可用性由各业务接口校验。"""

from io import BytesIO
from urllib.parse import quote

from fastapi.responses import StreamingResponse


def report_download_response(content: str, content_type: str, filename_stem: str) -> StreamingResponse:
    extension = {"text/markdown": "md", "text/plain": "txt", "text/html": "html"}[content_type]
    filename = f"{filename_stem}.{extension}"
    return StreamingResponse(
        BytesIO(content.encode("utf-8")),
        media_type=content_type + "; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )
