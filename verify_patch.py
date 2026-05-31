import json, base64, gzip, re

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
                except:
                    pass
            text = data.decode('utf-8', errors='ignore')
            idx = text.find('window.__INJECTED_TICKETS')
            if idx != -1:
                print('Found INJECTED:', text[idx-20:idx+40])
