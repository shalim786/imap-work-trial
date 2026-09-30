"""MIME descriptions with section literals extracted from original octets."""
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses
import re

def nstring(value):
    if value is None: return b'NIL'
    raw=str(value).encode('utf-8')
    if any(c<32 or c>=127 for c in raw): return b'{'+str(len(raw)).encode()+b'}\r\n'+raw
    return b'"'+raw.replace(b'\\',b'\\\\').replace(b'"',b'\\"')+b'"'

def wire_list(items): return b'('+b' '.join(items)+b')'

def split_raw(raw):
    match=re.search(br'\r\n\r\n|\n\n',raw)
    if not match: return raw,b'',b'\r\n'
    ending=b'\r\n' if match.group().startswith(b'\r') else b'\n'
    return raw[:match.end()],raw[match.end():],ending

class RawMessage:
    def __init__(self,raw):
        self.raw=raw; self.message=BytesParser(policy=policy.compat32).parsebytes(raw)
        self.header,self.text,self.ending=split_raw(raw)
        self.children=[]; self.embedded=None
        if self.message.get_content_maintype()=='multipart':
            boundary=self.message.get_boundary()
            if boundary:
                marker=b'--'+boundary.encode('ascii',errors='strict')
                lines=list(re.finditer(br'(?m)^'+re.escape(marker)+br'(--)?[ \t]*(?:\r?\n|$)',self.text))
                for i,line in enumerate(lines):
                    if line[1] or i+1>=len(lines): break
                    start=line.end(); stop=lines[i+1].start()
                    # The CRLF immediately preceding a boundary belongs to the delimiter.
                    if self.text[max(start,stop-2):stop]==b'\r\n': stop-=2
                    elif stop>start and self.text[stop-1:stop]==b'\n': stop-=1
                    self.children.append(RawMessage(self.text[start:stop]))
        elif self.message.get_content_type()=='message/rfc822': self.embedded=RawMessage(self.text)
    def section(self,section='',partial=None):
        section=section.upper(); match=re.match(r'^([1-9][0-9]*(?:\.[1-9][0-9]*)*)(?:\.(.*))?$',section)
        node=self; suffix=section; numbered=False
        if match:
            numbered=True; suffix=match[2] or ''
            for index,part in enumerate(match[1].split('.')):
                number=int(part)
                encapsulated=bool(index and node.embedded)
                if encapsulated: node=node.embedded
                if node.children:
                    if number>len(node.children): return None
                    node=node.children[number-1]
                elif number!=1 or (index and not encapsulated): return None
            if suffix and suffix!='MIME':
                if not node.embedded: return None
                node=node.embedded
        if suffix=='': result=node.text if numbered else node.raw
        elif suffix in ('HEADER','MIME'): result=node.header
        elif suffix=='TEXT': result=node.text
        else:
            fields=re.fullmatch(r'HEADER\.FIELDS(\.NOT)?\s+\(([^()]*)\)',suffix)
            if not fields: raise ValueError('Unsupported section')
            names={n.lower().encode() for n in fields[2].split()}; chunks=[]
            # Preserve folding, order, duplicate fields, spelling, and original line endings.
            content=node.header
            separator=node.ending
            for field in re.split(br'(?m)(?=^[^ \t\r\n][^\r\n]*:)',content):
                name=field.split(b':',1)[0].lower()
                if b':' not in field: continue
                if (name in names) != bool(fields[1]): chunks.append(field.rstrip(b'\r\n')+separator)
            result=b''.join(chunks)+separator
        if partial and result is not None: result=result[partial[0]:partial[0]+partial[1]]
        return result
    def envelope(self):
        m=self.message
        def addresses(name,fallback=None):
            values=m.get_all(name) or (m.get_all(fallback) if fallback else None)
            if not values: return b'NIL'
            items=[]
            # Header registry preserves RFC group begin/end markers, which getaddresses flattens.
            from email.headerregistry import HeaderRegistry
            try:
                parsed=HeaderRegistry()(name, ', '.join(str(v) for v in values))
                for group in parsed.groups:
                    if group.display_name is not None:
                        items.append(wire_list([b'NIL',b'NIL',nstring(group.display_name),b'NIL']))
                    for address in group.addresses:
                        items.append(wire_list([nstring(address.display_name or None),b'NIL',nstring(address.username),nstring(address.domain or None)]))
                    if group.display_name is not None:
                        items.append(wire_list([b'NIL',b'NIL',b'NIL',b'NIL']))
            except (ValueError,AttributeError,IndexError):
                for personal,address in getaddresses(values):
                    mailbox,sep,host=address.rpartition('@')
                    if not sep: mailbox=address; host=None
                    items.append(wire_list([nstring(personal or None),b'NIL',nstring(mailbox),nstring(host)]))
            return wire_list(items) if items else b'NIL'
        return wire_list([nstring(m.get('Date')),nstring(m.get('Subject')),addresses('From'),addresses('Sender','From'),addresses('Reply-To','From'),addresses('To'),addresses('Cc'),addresses('Bcc'),nstring(m.get('In-Reply-To')),nstring(m.get('Message-ID'))])
    def bodystructure(self,extended=True):
        m=self.message
        def parameters(header):
            params=m.get_params(header=header,failobj=[])[1:]
            return wire_list([nstring(x) for key,value in params for x in (key.upper(),value)]) if params else b'NIL'
        def disposition():
            value=m.get_content_disposition()
            return wire_list([nstring(value.upper()),parameters('content-disposition')]) if value else b'NIL'
        def language():
            value=m.get('Content-Language')
            if not value: return b'NIL'
            langs=[x.strip() for x in value.split(',')]
            return nstring(langs[0]) if len(langs)==1 else wire_list([nstring(x) for x in langs])
        if self.children:
            fields=[child.bodystructure(extended) for child in self.children]+[nstring(m.get_content_subtype().upper())]
            if extended: fields += [parameters('content-type'),disposition(),language(),nstring(m.get('Content-Location'))]
        else:
            main=m.get_content_maintype().upper(); sub=m.get_content_subtype().upper()
            fields=[nstring(main),nstring(sub),parameters('content-type'),nstring(m.get('Content-ID')),nstring(m.get('Content-Description')),nstring(m.get('Content-Transfer-Encoding','7BIT').upper()),str(len(self.text)).encode()]
            if main=='TEXT': fields.append(str(self.text.count(b'\n')).encode())
            elif main=='MESSAGE' and sub=='RFC822' and self.embedded:
                fields += [self.embedded.envelope(),self.embedded.bodystructure(extended),str(self.text.count(b'\n')).encode()]
            if extended: fields += [nstring(m.get('Content-MD5')),disposition(),language(),nstring(m.get('Content-Location'))]
        return wire_list(fields)
