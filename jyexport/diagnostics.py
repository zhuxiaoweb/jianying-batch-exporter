from __future__ import annotations
import json
import platform
import subprocess
import tempfile
from pathlib import Path
from .codec import Codec
from .discovery import app_dirs, roots_and_index, scan
from .storage import atomic_json, now, sha

def diagnose(hint, output):
    versions = app_dirs(hint)
    roots,index = roots_and_index(hint)
    drafts,warnings = scan(roots,index)
    encrypted = next((x for x in drafts if x.encrypted),None)
    sample = {'status':'没有加密样本'}
    if encrypted and versions:
        before = sha(encrypted.content)
        codec = Codec(versions[0])
        try:
            value = codec.read(encrypted.content)
            sample = {'status':'可读取','keys':list(value)}
        except Exception as exc:
            sample = {'status':'受限','reason':str(exc)}
        finally:
            codec.close()
        sample['source_unchanged'] = before == sha(encrypted.content)
    cli_result = {'status':'无可用内置CLI'}
    if versions and (Path(versions[0])/'jianying-agent-cli.exe').is_file():
        with tempfile.TemporaryDirectory(prefix='jyexport_probe_') as temp:
            input_path=Path(temp)/'input.json'
            input_path.write_text('{}','utf-8')
            try:
                check = subprocess.run([str(Path(versions[0])/'jianying-agent-cli.exe'),'draft','query-content','--input',str(input_path)],capture_output=True,encoding='utf-8',errors='replace',timeout=20,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                response = json.loads(check.stdout)
                cli_result = {k:response.get(k) for k in ('ok','code','message')}
            except Exception as exc:
                cli_result = {'status':'不可用','error':str(exc)}
    result = {'at':now(),'os':platform.platform(),'versions':versions,'roots':roots,'projects':len({x.folder for x in drafts}),
        'timelines':len(drafts),'encrypted_timelines':sum(x.encrypted for x in drafts),'readable_timelines':sum(not x.encrypted for x in drafts),
        'native_codec_sample':sample,'warnings':warnings,'drafts':[x.dict() for x in drafts],
        'native_cli_result':cli_result,
        'render_test':'未执行。本诊断仅读取文件与查询组件，不渲染视频。',
        'compatibility':'Windows 10/11 x64；剪映草稿与素材格式需逐版本验证。'}
    atomic_json(Path(output),result)
    if hasattr(__import__('sys').stdout,'write'):
        print(json.dumps({key:result[key] for key in ('projects','timelines','encrypted_timelines','readable_timelines','native_codec_sample')},ensure_ascii=True))
