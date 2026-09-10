"""Read the shared M3 token, including its existing root:10001 0640 mount."""
import os
from pathlib import Path
import re
import stat


def read_token_file(path):
    try:
        descriptor=os.open(Path(path),os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor,'r',encoding='utf-8') as stream:
            info=os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode)
                    or stat.S_IMODE(info.st_mode) not in (0o400,0o600,0o440,0o640)
                    or info.st_uid not in (0,os.geteuid())):
                raise ValueError('shared token file permissions invalid')
            if info.st_mode & 0o040 and info.st_gid not in {os.getegid(),*os.getgroups()}:
                raise ValueError('shared token file group is not available to this process')
            content=stream.read(8193)
        lines=[line.strip() for line in content.splitlines() if line.strip()]
        if len(content)>8192 or len(lines)!=1 or re.fullmatch(r'[\x21-\x7e]{16,4096}',lines[0]) is None:
            raise ValueError('shared token file content invalid')
        return lines[0]
    except (OSError,UnicodeError) as error:
        raise ValueError('shared token file cannot be read') from error
