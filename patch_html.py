import json, base64, gzip, re

with open('CS Chatbot Dashboard _standalone_.html', 'r', encoding='utf-8') as f:
    html = f.read()

match = re.search(r'<script type="__bundler/manifest">(.*?)</script>', html, re.DOTALL)
if match:
    manifest = json.loads(match.group(1))
    modified = False
    for k, v in manifest.items():
        if v.get('mime') in ['text/javascript', 'text/babel', 'application/javascript']:
            data = base64.b64decode(v['data'])
            if v.get('compressed'):
                try:
                    data = gzip.decompress(data)
                except Exception:
                    pass
            text = data.decode('utf-8', errors='ignore')
            
            if 'const TICKETS =' in text or 'const TICKETS=' in text or 'const TICKETS ' in text:
                print(f'Found TICKETS in {k}')
                # Replace it
                text = re.sub(r'const TICKETS\s*=', 'const TICKETS = window.__INJECTED_TICKETS ||', text)
                
                # Re-encode
                new_data = text.encode('utf-8')
                if v.get('compressed'):
                    new_data = gzip.compress(new_data)
                v['data'] = base64.b64encode(new_data).decode('utf-8')
                modified = True
                
    if modified:
        new_manifest_str = json.dumps(manifest)
        new_html = html[:match.start(1)] + new_manifest_str + html[match.end(1):]
        with open('CS Chatbot Dashboard _standalone_.html', 'w', encoding='utf-8') as f:
            f.write(new_html)
        print('Successfully patched HTML bundle to use window.__INJECTED_TICKETS')
    else:
        print('TICKETS declaration not found in bundle.')
