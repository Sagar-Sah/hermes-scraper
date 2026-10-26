"""Queue bridge to normal durable browser Runs, with injected application APIs.

No app/store import, network call or source arithmetic. Callbacks validate JobSpec,
lookup/create the owned Run, read saved status, and use ordinary pause/resume.
"""
from __future__ import annotations
import asyncio
from copy import deepcopy
import hashlib,json,math,os,re,tempfile,time
from pathlib import Path
from urllib.parse import urlsplit

PROFILE_ID='printograph_modern_browser_v1'
MODE='native_visible_domain_v1'
PDF_PROFILE_ID='printograph_manual_pdf_browser_v1'
PDF_MODE='native_visible_pdf_domain_v1'
from .printograph_modern_browser import PDF_SERVICE_POLICY, PDF_SOURCE_URLS

def _instance_scope(profile_id,mode,service_policy):
    if (type(profile_id) is not str or type(mode) is not str or service_policy is not None and type(service_policy) is not str
            or (profile_id,mode,service_policy) not in {(PROFILE_ID,MODE,None),(PDF_PROFILE_ID,PDF_MODE,PDF_SERVICE_POLICY)}):
        raise ValueError('unsupported_browser_profile_scope')


def normalize_configuration(value,*,profile_id=PROFILE_ID,mode=MODE,service_policy=None):
    _instance_scope(profile_id,mode,service_policy)
    expected={'mode':mode}
    if service_policy is not None:expected['service_policy']=service_policy
    if type(value) is not dict or value!=expected:raise ValueError('unsupported_browser_profile_configuration')
    return dict(value)

def supports(association):
    if type(association) is not dict or set(association)!={'campaign_id','product_id','supplier','source_url'}:return False
    u=urlsplit(str(association.get('source_url','')))
    return (association.get('supplier')=='printograph' and u.scheme=='https' and u.netloc in {'www.printograph.com','printograph.com'}
            and not u.query and not u.fragment and bool(re.fullmatch(r'/products/[A-Za-z0-9_-]+/order/?',u.path))
            and all(type(association[k]) is str and re.fullmatch(r'[A-Za-z0-9_.-]{1,128}',association[k]) for k in ('campaign_id','product_id')))

def discover(association,max_jobs=99):
    return {'configurations':[{'mode':MODE}], 'blockers':[]} if supports(association) and type(max_jobs) is int and 1<=max_jobs<=99 else {'configurations':[], 'blockers':['needs_profile']}

def _bytes(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False).encode()
def _sha(value):return hashlib.sha256(value).hexdigest()

def job_spec(association,owner,*,profile_id=PROFILE_ID,mode=MODE,service_policy=None):
    _instance_scope(profile_id,mode,service_policy)
    if service_policy is not None and (type(association) is not dict or association.get('source_url') not in PDF_SOURCE_URLS):raise ValueError('unsupported_browser_profile_source')
    if not supports(association) or not re.fullmatch(r'[a-f0-9]{64}',owner):raise ValueError('invalid_browser_owner')
    result={'name':'Campaign native browser '+association['product_id'],'urls':[association['source_url']],
        'adapter':'browser','instructions':'','provider_id':None,'filters':{},'image_mode':'urls',
        'shipping_contexts':[],'browser_bulk_quantities':False,'refresh':False,
        'source_metadata':{'printograph_modern_services':True,'automation_owner':owner,
            'automation_association':deepcopy(association)}}
    if service_policy is not None:
        result['source_metadata'].update(printograph_modern_service_policy=service_policy,automation_profile=profile_id)
    return result

class BrowserRunProfile:
    """One owned durable Run; finite chunks resume it, never create another.

    find_owned(owner) must read saved Runs by exact metadata owner. create(spec)
    is the application's ordinary validated JobSpec entry followed immediately
    by ordinary pause, returning the paused child before yielding to its Engine. read(id) includes the
    exact spec and products. control(id,'pause'/'resume') uses ordinary controls.
    No callback may reinterpret a completed/partial product as a current quote.
    """
    def __init__(self,root,*,create,read,control,find_owned,clock=time.monotonic,sleep=asyncio.sleep,chunk_seconds=90,poll_seconds=.5,profile_id=PROFILE_ID,mode=MODE,service_policy=None):
        if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (chunk_seconds,poll_seconds)) or not 0<chunk_seconds<=300 or not 0<poll_seconds<=5:raise ValueError('invalid_browser_chunk_bound')
        _instance_scope(profile_id,mode,service_policy)
        self.profile_id,self.mode,self.service_policy=profile_id,mode,service_policy
        self.root=Path(root).resolve();self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.create,self.read,self.control,self.find=create,read,control,find_owned
        self.clock,self.sleep,self.chunk,self.poll=clock,sleep,chunk_seconds,poll_seconds
    def supports(self,association):
        return supports(association) and (self.service_policy is None or association['source_url'] in PDF_SOURCE_URLS)
    def normalize_configuration(self,value):
        return normalize_configuration(value,profile_id=self.profile_id,mode=self.mode,service_policy=self.service_policy)
    def discover(self,association,max_jobs=99):
        if not self.supports(association) or type(max_jobs) is not int or not 1<=max_jobs<=99:return {'configurations':[],'blockers':['needs_profile']}
        value={'mode':self.mode}
        if self.service_policy is not None:value['service_policy']=self.service_policy
        return {'configurations':[value],'blockers':[]}
    def job_spec(self,association,owner):
        return job_spec(association,owner,profile_id=self.profile_id,mode=self.mode,service_policy=self.service_policy)
    def _write(self,path,value):
        data=_bytes(value);fd,name=tempfile.mkstemp(prefix='.browser-',dir=self.root)
        try:
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'wb') as f:f.write(data);f.flush();os.fsync(f.fileno())
            os.replace(name,path)
        finally:
            if os.path.exists(name):os.unlink(name)
    def _result(self,owner,association,run_id,status,count,reason=None):
        value={'schema':'printograph-owned-browser-stage/v1','owner':owner,'association':association,
            'harvest_id':run_id,'status':status,'scoped_row_count':count,'reason':reason,
            'stage_scoped_only':True,'source_data_finalized':False,'woocommerce_import_ready':False}
        if self.service_policy is not None:value.update(profile_id=self.profile_id,service_policy=self.service_policy)
        blob=_bytes(value);digest=_sha(blob);path=self.root/(digest+'.json')
        if path.exists():
            if path.read_bytes()!=blob:raise ValueError('browser_result_identity_conflict')
        else:
            fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(fd,'wb') as f:f.write(blob);f.flush();os.fsync(f.fileno())
        return str(path)+'#'+digest,digest
    def _validate_identity(self,run,association,owner,run_id=None):
        if type(run) is not dict or not re.fullmatch(r'[a-f0-9]{32}',str(run.get('id',''))):raise ValueError('invalid_owned_browser_run')
        if run_id is not None and run['id']!=run_id:raise ValueError('browser_run_identity_changed')
        expected=self.job_spec(association,owner);spec=run.get('spec')
        if type(spec) is not dict or any(spec.get(k)!=v for k,v in expected.items()):raise ValueError('browser_run_scope_changed')
        products=run.get('products')
        if not isinstance(products,list) or len(products)!=1 or products[0].get('url')!=association['source_url']:raise ValueError('browser_run_product_scope_changed')
    def _validate(self,run,association,owner,run_id=None):
        self._validate_identity(run,association,owner,run_id)
        count=run.get('scoped_row_count')
        if count is None and run.get('row_count')==0:count=0
        if type(count) is not int or not 0<=count<=10_000_000:raise ValueError('browser_scoped_rows_unknown')
        return count
    async def execute(self,task,check_scope):
        association=deepcopy(task['association']);self.normalize_configuration(task['configuration'])
        if not self.supports(association):return {'state':'held','reason':'needs_profile'}
        owner=_sha(_bytes({'profile':self.profile_id,'association':association,'configuration':task['configuration']}))
        path=self.root/(owner+'.owner.json');run_id=None;terminal=False;cursor=None;created_now=False
        try:
            await check_scope()
            if path.exists():
                cursor=json.loads(path.read_bytes())
                if type(cursor) is not dict or cursor.get('owner')!=owner or cursor.get('association')!=association or type(cursor.get('rows')) is not int or not 0<=cursor['rows']<=10_000_000 or cursor.get('intent') not in {'creating','owned','paused','completed'} or cursor.get('run_id') is not None and not re.fullmatch(r'[a-f0-9]{32}',str(cursor['run_id'])):raise ValueError('browser_owner_scope_changed')
            else:
                cursor={'owner':owner,'association':association,'run_id':None,'rows':0,'intent':'creating'}
                self._write(path,cursor)
            found=await self.find(owner)
            if not isinstance(found,list) or len(found)>1:raise ValueError('browser_owner_lookup_ambiguous')
            if found:
                self._validate_identity(found[0],association,owner,cursor['run_id'])
                run_id=found[0]['id']
            await check_scope()
            if cursor['run_id']:
                if len(found)!=1 or found[0].get('id')!=cursor['run_id']:raise ValueError('browser_owned_run_missing')
                run=found[0]
            elif found:run=found[0]
            elif cursor.get('creation_attempted') is True:
                return {'state':'held','reason':'browser_creation_outcome_unknown'}
            else:
                cursor['creation_attempted']=True;self._write(path,cursor)
                # Metadata owner makes an interrupted creation findable. Never
                # retry create after unknown outcome; only the saved lookup.
                await check_scope()
                run=await self.create(self.job_spec(association,owner))
                created_now=True
            self._validate_identity(run,association,owner,cursor['run_id'])
            run_id=run['id']
            count=self._validate(run,association,owner,cursor['run_id'])
            if created_now and run.get('status')!='paused':raise ValueError('browser_child_not_created_paused')
            await check_scope()
            prior_intent=cursor['intent']
            cursor['run_id']=run_id;cursor['intent']='owned';self._write(path,cursor)
            if run['status']=='paused' and prior_intent!='paused' and not created_now:
                return {'state':'held','reason':'browser_owned_run_paused'}
            if run['status']=='paused':
                await check_scope();run=await self.control(run_id,'resume');await check_scope();self._validate(run,association,owner,run_id)
            start=self.clock()
            if not math.isfinite(start):raise ValueError('browser_chunk_clock_invalid')
            while True:
                await check_scope()
                run=await self.read(run_id);await check_scope();count=self._validate(run,association,owner,run_id)
                if count<cursor['rows']:raise ValueError('browser_scoped_rows_regressed')
                status=run['status'];products=run['products']
                if status=='completed':
                    terminal=True
                    if not(count>0 and run.get('coverage_status')=='complete' and all(p.get('status')=='completed' and p.get('coverage_status')=='complete' for p in products)):
                        return {'state':'held','reason':'browser_coverage_incomplete'}
                    ref,_=self._result(owner,association,run_id,'native_visible_domain_exhausted',count)
                    cursor['rows']=count;cursor['intent']='completed';self._write(path,cursor)
                    return {'state':'completed','result_reference':ref}
                if status in {'completed_with_gaps','failed','cancelled'} or any(p.get('status') in {'partial','needs_attention','access_required','failed','cancelled'} for p in products):
                    terminal=True
                    state='access_required' if any(p.get('status')=='access_required' for p in products) else 'held'
                    ref,_=self._result(owner,association,run_id,'held_partial',count,'native_browser_coverage_incomplete')
                    return {'state':state,'reason':'supplier_authentication_required' if state=='access_required' else 'native_browser_coverage_incomplete','result_reference':ref}
                if status not in {'queued','running','exporting','paused'}:raise ValueError('unknown_browser_run_state')
                elapsed=self.clock()-start
                if not math.isfinite(elapsed) or elapsed<0:raise ValueError('browser_chunk_clock_rollback')
                if elapsed>=self.chunk:
                    await self.control(run_id,'pause')
                    run=await self.read(run_id);await check_scope();count=self._validate(run,association,owner,run_id)
                    if count<=cursor['rows']:return {'state':'held','reason':'browser_chunk_no_durable_progress'}
                    ref,token=self._result(owner,association,run_id,'paused_advancing_chunk',count)
                    cursor['rows']=count;cursor['intent']='paused';self._write(path,cursor)
                    return {'state':'continue','result_reference':ref,'progress_count':count,'progress_token':token,'delay_seconds':0}
                await self.sleep(self.poll)
        finally:
            if not terminal:
                async def cleanup():
                    owned_id=run_id
                    if owned_id is None and cursor is not None and cursor.get('creation_attempted') is True:
                        # create can fail after its durable commit. A source-free
                        # exact owner lookup is cleanup, never a create retry.
                        matches=await self.find(owner)
                        if isinstance(matches,list) and len(matches)==1:
                            self._validate_identity(matches[0],association,owner,cursor.get('run_id'))
                            owned_id=matches[0]['id']
                    if owned_id is not None:
                        await self.control(owned_id,'pause')
                # Cancellation/parent pause must await the bounded saved-state
                # cleanup; only a unique validated owned child can be paused.
                await asyncio.shield(cleanup())
    def profile(self):
        from .campaign_automation import Profile
        return Profile(self.profile_id,'printograph',self.supports,self.discover,self.normalize_configuration,self.execute,priority=10 if self.service_policy is not None else 0)
