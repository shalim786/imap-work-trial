"""Generate an untrusted localhost certificate for isolated tests, never for deployment."""
import argparse
import os
from pathlib import Path
import subprocess
import tempfile

def create_certificate(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    cert, key = directory/'localhost.crt', directory/'localhost.key'
    if cert.exists() or key.exists():
        raise ValueError('Refusing to overwrite existing certificate/key')
    config = '''[req]
prompt=no
distinguished_name=dn
x509_extensions=extensions
[dn]
CN=localhost
[extensions]
subjectAltName=DNS:localhost,IP:127.0.0.1,IP:::1
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
'''
    with tempfile.TemporaryDirectory() as temporary:
        cfg=Path(temporary)/'openssl.cnf';cfg.write_text(config)
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','2',
                        '-config',str(cfg),'-keyout',str(key),'-out',str(cert)],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    os.chmod(key,0o600)
    return cert,key

if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--directory',default='var/tls-test')
    args=parser.parse_args()
    cert,key=create_certificate(args.directory)
    print(f'Test certificate: {cert}\nPrivate key: {key}\nNot trusted by Thunderbird or the system trust store.')
