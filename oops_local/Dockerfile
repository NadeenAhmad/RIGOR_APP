# Local OOPS! (OntOlogy Pitfall Scanner) for RIGOR.
#
# The published image (mpovedavillalon/oops, by the OOPS! authors) ships
# without the WordNet 3.0 dictionary that several pitfall checks read, so it
# answers "OOPS! something went wrong" for most real ontologies. This adds:
#   - the WordNet 3.0 database files (NLTK's distribution), and
#   - sents.vrb / sentidx.vrb (Debian wordnet-base) and frames.vrb, which the
#     NLTK copy omits.
FROM mpovedavillalon/oops:v2

ADD https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip /tmp/wordnet.zip
RUN mkdir -p /usr/local/tomcat/WordNet/WordNet-3.0/dict \
 && cd /tmp && unzip -q wordnet.zip \
 && cp wordnet/* /usr/local/tomcat/WordNet/WordNet-3.0/dict/ \
 && rm -rf /tmp/wordnet /tmp/wordnet.zip
COPY wordnet_extra/ /usr/local/tomcat/WordNet/WordNet-3.0/dict/

# Use Tomcat's plain Java connector over IPv4; the image's native APR
# connector fails to bind on hosts without IPv6.
ENV LD_LIBRARY_PATH="" JAVA_OPTS="-Djava.net.preferIPv4Stack=true"
