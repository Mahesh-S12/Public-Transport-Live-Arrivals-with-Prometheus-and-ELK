FROM docker.elastic.co/beats/filebeat:7.17.14
COPY filebeat.yml /usr/share/filebeat/filebeat.yml

