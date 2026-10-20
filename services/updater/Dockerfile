



FROM docker:27-cli@sha256:851f91d241214e7c6db86513b270d58776379aacc5eb9c4a87e5b47115e3065c




RUN apk add --no-cache curl openssl jq

COPY updater.sh /updater.sh

COPY release.pub.pem /release.pub.pem
RUN chmod +x /updater.sh && chmod 444 /release.pub.pem





ENTRYPOINT ["/updater.sh"]
