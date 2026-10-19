FROM python:3.13-slim
RUN pip install --no-cache-dir openpyxl==3.1.5 xlrd==2.0.2 pypdf==5.9.0 Pillow==11.3.0 pandas==2.3.2 python-docx==1.2.0 defusedxml==0.7.1
COPY runner.py /opt/runner.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER 65534:65534
WORKDIR /work
ENTRYPOINT ["python", "-I", "/opt/runner.py"]
