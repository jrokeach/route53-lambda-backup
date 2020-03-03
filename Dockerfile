FROM python:3

ADD route53_backup.py /
ADD requirements.txt /

RUN pip install -r requirements.txt

ENTRYPOINT python3 route53_backup.py
