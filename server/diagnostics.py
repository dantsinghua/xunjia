"""Bounded, owner-filtered operational evidence; never retains exception text."""
from copy import deepcopy
import logging
import re
import threading
import time


class Diagnostics:
    def __init__(self,clock=time.time):
        self.clock=clock
        self._lock=threading.Lock()
        self._entries={}

    def record(self,component,*,owner=None,code=None,error=None,entity=None):
        if not re.fullmatch(r'[a-z_]{1,40}',component):raise ValueError('Invalid diagnostic component')
        if code is not None and not re.fullmatch(r'[A-Z0-9_]{1,80}',code):raise ValueError('Invalid diagnostic code')
        now=self.clock();key=(owner,component)
        with self._lock:
            old=self._entries.get(key,{})
            value={**old,'component':component,'attempts':old.get('attempts',0)+1,'status':'degraded' if code else 'ok'}
            if code:
                value.update(code=code,error_type=type(error).__name__ if error else None,last_failure_at=now,
                             failures=old.get('failures',0)+1,consecutive_failures=old.get('consecutive_failures',0)+1,
                             entity_id=entity if owner else None)
            else:
                value.update(code=None,error_type=None,last_success_at=now,consecutive_failures=0)
            self._entries[key]=value
        if code:
            logging.getLogger('xunjia.runtime').warning('component=%s code=%s error_type=%s',component,code,type(error).__name__ if error else 'none')

    def snapshot(self,owner):
        with self._lock:
            return [deepcopy(value) for (who,_),value in self._entries.items() if who is None or who==owner]
