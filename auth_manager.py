
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

class AuthManager:
    def __init__(self, root, session_days=7):
        self.root=Path(root)
        self.runtime=self.root/"runtime"
        self.runtime.mkdir(parents=True,exist_ok=True)
        self.auth_file=self.runtime/"auth.json"
        self.session_days=int(session_days)
        self.lock=threading.RLock()
        self.sessions={}
        self.failures={}

    def configured(self):
        try:
            d=json.loads(self.auth_file.read_text(encoding="utf-8"))
            return bool(d.get("username") and d.get("salt") and d.get("password_hash"))
        except Exception:
            return False

    def _load(self):
        if not self.auth_file.exists():
            return {}
        return json.loads(self.auth_file.read_text(encoding="utf-8"))

    def _write(self,data):
        tmp=self.auth_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding="utf-8")
        os.replace(tmp,self.auth_file)

    @staticmethod
    def _hash(password,salt,iterations=310000):
        return hashlib.pbkdf2_hmac("sha256",password.encode("utf-8"),salt,int(iterations),dklen=32)

    def setup(self,username,password):
        with self.lock:
            if self.configured():
                raise RuntimeError("Authentication is already configured.")
            username=str(username or "").strip()
            password=str(password or "")
            if not username or len(username)>64:
                raise ValueError("Username length must be 1-64 characters.")
            if len(password)<8:
                raise ValueError("Password must be at least 8 characters.")
            salt=secrets.token_bytes(16)
            iterations=310000
            digest=self._hash(password,salt,iterations)
            self._write({
                "username":username,
                "salt":base64.b64encode(salt).decode("ascii"),
                "password_hash":base64.b64encode(digest).decode("ascii"),
                "iterations":iterations,
                "created_at":int(time.time())
            })

    def verify(self,username,password):
        try:
            d=self._load()
            password=str(password or "")
            if str(username or "")!=d.get("username"):
                self._hash(password,b"\0"*16,310000)
                return False
            actual=self._hash(password,base64.b64decode(d["salt"]),int(d.get("iterations",310000)))
            expected=base64.b64decode(d["password_hash"])
            return hmac.compare_digest(actual,expected)
        except Exception:
            return False

    def username(self):
        try:return self._load().get("username","")
        except Exception:return ""

    def create_session(self,username):
        token=secrets.token_urlsafe(32)
        now=time.time()
        exp=now+self.session_days*86400
        with self.lock:
            self._purge(now)
            self.sessions[token]={"username":username,"created_at":now,"expires_at":exp}
        return token,int(exp)

    def validate_session(self,token):
        if not token:return None
        now=time.time()
        with self.lock:
            self._purge(now)
            s=self.sessions.get(token)
            return dict(s) if s else None

    def logout(self,token):
        with self.lock:self.sessions.pop(token,None)

    def _purge(self,now):
        for k in [k for k,v in self.sessions.items() if v.get("expires_at",0)<=now]:
            self.sessions.pop(k,None)

    def _state(self,ip):
        now=time.time()
        with self.lock:
            s=self.failures.get(ip,{"attempts":[],"blocked_until":0})
            s["attempts"]=[t for t in s.get("attempts",[]) if now-t<=60]
            self.failures[ip]=s
            return s

    def login_allowed(self,ip):
        s=self._state(ip)
        now=time.time()
        if s.get("blocked_until",0)>now:
            return False,max(1,int(s["blocked_until"]-now))
        return True,0

    def register_failure(self,ip):
        now=time.time()
        with self.lock:
            s=self._state(ip)
            s["attempts"].append(now)
            if len(s["attempts"])>=5:
                s["blocked_until"]=now+30
            self.failures[ip]=s
        return self.login_allowed(ip)

    def clear_failures(self,ip):
        with self.lock:self.failures.pop(ip,None)
