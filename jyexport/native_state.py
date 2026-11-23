"""Write-ahead state journal for reversible native-editor operations."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
from .storage import atomic_json, now, sha
from .media import read_srt


class NativeJournal:
    def __init__(self, path, data):
        self.path=Path(path).resolve()
        self.data=data

    @classmethod
    def create(cls, path, binding):
        path=Path(path).resolve()
        if path.exists():raise FileExistsError('运行记录已存在，请恢复该记录而非覆盖')
        path.parent.mkdir(parents=True,exist_ok=True)
        data={'schema':1,'run_id':uuid.uuid4().hex,'binding':binding,'created':now(),
              'state':'preflight','original':{},'observed':{},'pending':{},'events':[],
              'exports':{},'restored':False,'complete':False}
        journal=cls(path,data);journal.save();return journal

    @classmethod
    def open(cls,path):
        path=Path(path).resolve()
        data=json.loads(path.read_text(encoding='utf-8'))
        if data.get('schema')!=1 or not isinstance(data.get('binding'),dict):
            raise ValueError('无法识别原生运行记录')
        for key in ('original','observed','pending','exports'):
            if not isinstance(data.get(key),dict):raise ValueError('运行记录状态不完整')
        return cls(path,data)

    def save(self):atomic_json(self.path,self.data)

    def event(self,action,**fields):
        self.data['events'].append({'at':now(),'action':action,**fields});self.save()

    def original(self,target,value,evidence):
        if self.data['state']!='preflight':raise ValueError('修改开始后不能重写初始状态')
        if type(value) is not bool or not evidence:raise ValueError('初始状态需要明确布尔值与观察证据')
        if target in self.data['original'] and self.data['original'][target]!=value:
            raise ValueError('预检期间状态变化，不能确认原状态')
        self.data['original'][target]=value;self.data['observed'][target]=value
        self.event('观察初始状态',target=target,value=value,evidence=evidence)

    def ready(self,targets):
        if self.data['state']!='preflight' or set(targets)!=set(self.data['original']):
            raise ValueError('全部目标预检完成前禁止修改')
        self.data['targets']=list(targets)
        self.data['state']='ready';self.event('全部目标预检通过')

    def intent(self,target,before,after,evidence):
        if self.data['state']=='preflight' or target not in self.data['original']:
            raise ValueError('目标尚未经过完整预检')
        if type(before)is not bool or type(after)is not bool or not evidence:
            raise ValueError('切换必须有明确的原生状态证据')
        if target in self.data['pending']:
            raise ValueError('上一动作结果未知，先观察并解决待确认状态')
        self.data.update(state='editing',restored=False,complete=False)
        self.data['pending'][target]={'before':before,'after':after,'at':now(),'evidence':evidence}
        self.data['observed'][target]=before
        self.event('切换意图已落盘',target=target,before=before,after=after,evidence=evidence)

    def observe(self,target,value,evidence):
        if target not in self.data['original'] or type(value)is not bool or not evidence:
            raise ValueError('不能登记未经预检或缺乏证据的状态')
        self.data['observed'][target]=value
        pending=self.data['pending'].pop(target,None)
        self.event('原生状态已复查',target=target,value=value,evidence=evidence,
                   intended=pending.get('after') if pending else None)

    def export(self,key,file,probe,evidence):
        if key not in ('V2','V3') or not evidence or not probe.get('full_decode'):
            raise ValueError('原生母版必须有全片解码与实际完成依据')
        file=Path(file).resolve()
        if not file.is_file() or file.stat().st_size<1024:raise ValueError('没有实际非空母版文件')
        if (probe.get('width'),probe.get('height'),probe.get('fps'))!=(1080,1920,'60') or probe.get('audio_streams',0)<1:
            raise ValueError('母版规格不合格')
        self.data['exports'][key]={'file':str(file),'sha256':sha(file),'probe':probe,'evidence':evidence}
        self.event('母版导出已核验',variant=key,file=str(file))

    def needs_recovery(self):
        return bool(self.data['pending'] or any(self.data['observed'].get(key)!=value
                    for key,value in self.data['original'].items()))

    def restored(self):
        if not self.data.get('targets') or self.needs_recovery():raise ValueError('尚有未确认或未恢复的状态，不能声称恢复完成')
        self.data['restored']=True;self.data['state']='restored';self.event('全部原状态已恢复并复查')

    def fail(self,error):
        if self.needs_recovery():
            state='needs_recovery'
        elif self.data.get('restored'):
            state='failed_restored'
        elif not any(event.get('action')=='切换意图已落盘' for event in self.data['events']):
            state='preflight_failed'
        else:
            state='failed_unconfirmed'
        self.data['state']=state
        self.data['error']=str(error);self.data['complete']=False
        self.event('运行失败',error=str(error),needs_recovery=self.needs_recovery())

    def finish(self,srt):
        if not self.data['restored'] or self.needs_recovery() or set(self.data['exports'])!={'V2','V3'} or not Path(srt).is_file():
            raise ValueError('两母版、SRT或状态恢复尚未完成')
        for item in self.data['exports'].values():
            if not Path(item['file']).is_file() or sha(item['file'])!=item['sha256']:
                raise ValueError('已验证的原生母版缺失或发生变化')
        read_srt(srt)
        self.data.update(state='completed',complete=True,srt=str(Path(srt).resolve()))
        self.event('原生两母版流程完成')
