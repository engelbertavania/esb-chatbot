import json
import re
import base64
import gzip

with open('CS Chatbot Dashboard _standalone_.html', 'r', encoding='utf-8') as f:
    html = f.read()

match = re.search(r'<script type="__bundler/manifest">(.*?)</script>', html, re.DOTALL)
if match:
    manifest = json.loads(match.group(1))
    for k, v in manifest.items():
        if v.get('mime') in ['text/javascript', 'text/babel', 'application/javascript']:
            data = base64.b64decode(v['data'])
            if v.get('compressed'):
                try:
                    data = gzip.decompress(data)
                except Exception as e:
                    pass
            text = data.decode('utf-8', errors='ignore')
            if 'fetch' in text or '/api' in text:
                print('Found in', k)
                lines = [line for line in text.split('\n') if 'fetch' in line or '/api' in line]
                print('\n'.join(lines[:20]))
