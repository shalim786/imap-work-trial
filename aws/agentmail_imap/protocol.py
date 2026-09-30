"""Bounded command syntax helpers for the IMAP4rev1 read surface."""
from dataclasses import dataclass
from datetime import datetime, date
import re

class ProtocolError(ValueError):
    pass

class UnsupportedCharset(ProtocolError):
    pass

def tokenize(text: str) -> list:
    """Tokenize a complete command, preserving BODY[...] as a single atom."""
    root=[]; stack=[root]; i=0
    while i < len(text):
        c=text[i]
        if c.isspace(): i+=1; continue
        if c=='(':
            if len(stack)>=32: raise ProtocolError('Nesting too deep')
            child=[]; stack[-1].append(child); stack.append(child); i+=1; continue
        if c==')':
            if len(stack)==1: raise ProtocolError('Unexpected closing parenthesis')
            stack.pop(); i+=1; continue
        if c=='"':
            i+=1; value=''
            while i<len(text) and text[i]!='"':
                if text[i]=='\\':
                    i+=1
                    if i==len(text) or text[i] not in ('\\','"'): raise ProtocolError('Invalid quote escape')
                if text[i] in '\r\n\x00': raise ProtocolError('Invalid quoted character')
                value+=text[i]; i+=1
            if i==len(text): raise ProtocolError('Unclosed quote')
            stack[-1].append(value); i+=1; continue
        start=i; bracket=0
        while i<len(text):
            c=text[i]
            if c=='[': bracket+=1
            elif c==']':
                bracket-=1
                if bracket<0: raise ProtocolError('Unexpected section bracket')
            if bracket==0 and (c.isspace() or c in '()'): break
            if c in '\r\n\x00': raise ProtocolError('Invalid atom')
            i+=1
        if bracket: raise ProtocolError('Unclosed section bracket')
        if i==start: raise ProtocolError('Invalid token')
        stack[-1].append(text[start:i])
    if len(stack)!=1: raise ProtocolError('Unclosed list')
    return root

def parse_sequence_set(spec: str, actual_values) -> list[int]:
    values=sorted(set(actual_values)); maximum=max(values,default=0); ranges=[]
    def number(s):
        if s=='*': return maximum
        if not re.fullmatch(r'[1-9][0-9]*',s) or int(s)>4294967295: raise ProtocolError('Invalid sequence set')
        return int(s)
    if not isinstance(spec,str) or not spec: raise ProtocolError('Invalid sequence set')
    for item in spec.split(','):
        ends=item.split(':')
        if len(ends)>2: raise ProtocolError('Invalid sequence range')
        a=number(ends[0]); b=number(ends[-1]); ranges.append((min(a,b),max(a,b)))
    return [v for v in values if any(a<=v<=b for a,b in ranges)]

@dataclass(frozen=True)
class FetchAttribute:
    name: str
    section: str|None=None
    peek: bool=False
    partial: tuple[int,int]|None=None
    @property
    def needs_raw(self): return self.section is not None or self.name in ('ENVELOPE','BODY','BODYSTRUCTURE','RFC822','RFC822.HEADER','RFC822.TEXT')
    @property
    def sets_seen(self): return self.name in ('RFC822','RFC822.TEXT') or (self.section is not None and not self.peek)
    @property
    def response_name(self):
        if self.section is None: return self.name
        return 'BODY['+self.section+']'+(f'<{self.partial[0]}>' if self.partial else '')

def parse_fetch_attributes(value) -> list[FetchAttribute]:
    tokens=tokenize(value) if isinstance(value,str) else value
    if len(tokens)==1 and isinstance(tokens[0],list): tokens=tokens[0]
    if not tokens: raise ProtocolError('Missing FETCH attributes')
    macros={'ALL':['FLAGS','INTERNALDATE','RFC822.SIZE','ENVELOPE'],'FAST':['FLAGS','INTERNALDATE','RFC822.SIZE'],'FULL':['FLAGS','INTERNALDATE','RFC822.SIZE','ENVELOPE','BODY']}
    if len(tokens)==1 and isinstance(tokens[0],str) and tokens[0].upper() in macros: tokens=macros[tokens[0].upper()]
    result=[]
    for token in tokens:
        if not isinstance(token,str): raise ProtocolError('Invalid FETCH attribute')
        upper=token.upper(); match=re.fullmatch(r'BODY(\.PEEK)?\[(.*)\](?:<([0-9]+)\.([1-9][0-9]*)>)?',upper)
        if match:
            section=match[2]
            if not re.fullmatch(r'(?:[1-9][0-9]*(?:\.[1-9][0-9]*)*(?:\.)?)?(?:HEADER(?:\.FIELDS(?:\.NOT)?\s+\([^()]+\))?|TEXT|MIME)?',section): raise ProtocolError('Invalid BODY section')
            if section=='MIME' or (section.endswith('.') and section): raise ProtocolError('Invalid BODY section')
            result.append(FetchAttribute('BODY',section,bool(match[1]),(int(match[3]),int(match[4])) if match[3] else None))
        elif upper in ('UID','FLAGS','INTERNALDATE','RFC822.SIZE','ENVELOPE','BODY','BODYSTRUCTURE','RFC822','RFC822.HEADER','RFC822.TEXT'):
            result.append(FetchAttribute(upper))
        else: raise ProtocolError('Unsupported FETCH attribute')
    return result

@dataclass
class SearchMessage:
    sequence: int
    uid: int
    flags: set[str]
    internaldate: datetime
    size: int
    raw: bytes|None=None
    max_sequence: int=0
    max_uid: int=0

@dataclass(frozen=True)
class SearchNode:
    key: str
    args: tuple=()
    @property
    def needs_raw(self):
        return self.key in ('HEADER','FROM','TO','CC','BCC','SUBJECT','BODY','TEXT','SENTBEFORE','SENTON','SENTSINCE') or any(isinstance(a,SearchNode) and a.needs_raw for a in self.args)
    def evaluate(self,m:SearchMessage):
        k=self.key; a=self.args
        if k=='AND': return all(x.evaluate(m) for x in a)
        if k=='OR': return any(x.evaluate(m) for x in a)
        if k=='NOT': return not a[0].evaluate(m)
        if k=='ALL': return True
        flags={f.upper() for f in m.flags}
        if k=='NEW': return '\\RECENT' in flags and '\\SEEN' not in flags
        if k=='OLD': return '\\RECENT' not in flags
        names={'ANSWERED','DELETED','DRAFT','FLAGGED','RECENT','SEEN'}
        if k in names: return '\\'+k in flags
        if k.startswith('UN') and k[2:] in names: return '\\'+k[2:] not in flags
        if k in ('KEYWORD','UNKEYWORD'): return (a[0].upper() in flags)==(k=='KEYWORD')
        if k in ('LARGER','SMALLER'): return m.size> a[0] if k=='LARGER' else m.size<a[0]
        if k in ('UID','SEQUENCE'):
            n=m.uid if k=='UID' else m.sequence; maximum=m.max_uid if k=='UID' else m.max_sequence
            return n in parse_sequence_set(a[0],[n,maximum] if maximum else [n])
        if k in ('BEFORE','ON','SINCE'): d=m.internaldate.date()
        else:
            from .mime import RawMessage
            if m.raw is None: raise ValueError('Search requires raw message')
            raw=RawMessage(m.raw); msg=raw.message
            if k in ('HEADER','FROM','TO','CC','BCC','SUBJECT'):
                field,needle=a if k=='HEADER' else (k,a[0])
                from email.header import decode_header, make_header
                return any(needle.casefold() in str(make_header(decode_header(str(v)))).casefold() for v in msg.get_all(field,[]))
            if k in ('BODY','TEXT'):
                texts=[]
                if k=='TEXT':
                    from email.header import decode_header, make_header
                    texts.extend(str(make_header(decode_header(str(v)))) for _,v in msg.items())
                for part in msg.walk():
                    if part.get_content_maintype()=='text':
                        content=part.get_payload(decode=True) or b''
                        texts.append(content.decode(part.get_content_charset() or 'utf-8',errors='replace'))
                return a[0].casefold() in '\n'.join(texts).casefold()
            from email.utils import parsedate_to_datetime
            try: d=parsedate_to_datetime(str(msg.get('Date',''))).date()
            except (ValueError,TypeError,OverflowError): return False
        return d<a[0] if k.endswith('BEFORE') or k=='BEFORE' else d==a[0] if k.endswith('ON') or k=='ON' else d>=a[0]

def parse_search(tokens, charset=None) -> SearchNode:
    tokens=tokenize(tokens) if isinstance(tokens,str) else list(tokens)
    if tokens and isinstance(tokens[0],str) and tokens[0].upper()=='CHARSET':
        if len(tokens)<3: raise ProtocolError('Missing charset or criteria')
        charset=tokens[1]; tokens=tokens[2:]
    if charset and charset.upper() not in ('US-ASCII','UTF-8'): raise UnsupportedCharset('Supported charsets: US-ASCII UTF-8')
    def group(items):
        items=list(items); out=[]
        def one():
            if not items: raise ProtocolError('Missing SEARCH operand')
            t=items.pop(0)
            if isinstance(t,list): return group(t)
            k=t.upper()
            if k in ('OR','NOT'): return SearchNode(k,tuple(one() for _ in range(2 if k=='OR' else 1)))
            if k in ('ALL','ANSWERED','DELETED','DRAFT','FLAGGED','RECENT','SEEN','UNANSWERED','UNDELETED','UNDRAFT','UNFLAGGED','UNSEEN','NEW','OLD'): return SearchNode(k)
            count=2 if k=='HEADER' else 1
            if k not in ('HEADER','FROM','TO','CC','BCC','SUBJECT','BODY','TEXT','KEYWORD','UNKEYWORD','LARGER','SMALLER','UID','BEFORE','ON','SINCE','SENTBEFORE','SENTON','SENTSINCE'):
                parse_sequence_set(t,[]); return SearchNode('SEQUENCE',(t,))
            if len(items)<count or any(not isinstance(x,str) for x in items[:count]): raise ProtocolError('Missing SEARCH argument')
            args=items[:count]; del items[:count]
            if k in ('LARGER','SMALLER'):
                if not args[0].isdigit(): raise ProtocolError('Invalid search size')
                args[0]=int(args[0])
            elif k=='UID': parse_sequence_set(args[0],[])
            elif k in ('BEFORE','ON','SINCE','SENTBEFORE','SENTON','SENTSINCE'):
                try: args[0]=datetime.strptime(args[0],'%d-%b-%Y').date()
                except ValueError: raise ProtocolError('Invalid search date') from None
            return SearchNode(k,tuple(args))
        while items: out.append(one())
        if not out: raise ProtocolError('Missing SEARCH criteria')
        return SearchNode('AND',tuple(out))
    return group(tokens)
