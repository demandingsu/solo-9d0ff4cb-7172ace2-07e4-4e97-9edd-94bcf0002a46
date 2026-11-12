FROM python:3.11-slim

WORKDIR /srv/site
COPY html ./html

EXPOSE 8080
HEALTHCHECK --interval=10s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/index.html', timeout=3).status==200 else 1)"

# 静态公告站：标准库 http.server，无需任何第三方依赖
CMD ["python", "-m", "http.server", "8080", "--directory", "html"]
