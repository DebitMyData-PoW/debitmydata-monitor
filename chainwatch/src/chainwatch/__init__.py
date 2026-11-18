"""The DebitMyData chain watch: the monitoring server's own view of the DebitMyData chain.

It keeps every block in its database (back to block 1), works out the hashrate, the volume, the
rich list and the mining cost of DMD (the DebitMyData coin) from them, records the DebitMyData
Gateway's reference price (and the exchanges' figures once DMD trades on one), compares its node
with the other views of the chain (explorers, the pool's node), sends alerts and the stats page's
notifications, and writes the files of the public stats page and the numbers Prometheus reads.
"""

__version__ = "1.1.6"
