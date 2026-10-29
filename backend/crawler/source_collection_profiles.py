"""Generic authenticated source-collection profile router; public path unchanged."""
from pathlib import Path
from .campaign_sources import SourceCollector
from .wsdisplay_source_collector import WsdisplaySourceCollector, PROFILE_ID, Held

class SourceCollectionProfiles:
    def __init__(self,store,browser_pool):
        self.public=SourceCollector(store);self.native=WsdisplaySourceCollector(store,browser_pool)
    @property
    def _tasks(self):return {**self.public._tasks,**self.native.tasks}
    async def start(self,urls,*,profile_id=None,profile_options=None):
        if profile_id is None:
            if profile_options not in (None,{}):raise ValueError('Public collection has no profile options')
            return await self.public.start(urls)
        if profile_id!=PROFILE_ID:raise ValueError('Unsupported source collection profile')
        return await self.native.start(urls,profile_options or {})
    def owner(self,jid):
        native=self.native.path(jid).is_file();public=self.public._path(jid).is_file()
        if native and public:raise ValueError('Ambiguous source collection identifier')
        if native:return self.native
        if public:return self.public
        raise ValueError('Unknown source collection')
    def get(self,jid,*,include_data=False):return self.owner(jid).get(jid,include_data=include_data)
    status=get
    def list(self,limit=20):
        return sorted(self.public.list(limit)+self.native.list(limit),key=lambda j:j.get('updated_at',''),reverse=True)[:max(1,min(int(limit),100))]
    async def resume(self,jid,*,urls=None,max_nodes_per_product=None):
        owner=self.owner(jid)
        if urls is None and max_nodes_per_product is None:return await owner.resume(jid)
        return await owner.resume(jid,urls=urls,max_nodes_per_product=max_nodes_per_product)
    async def pause(self,jid):return await self.owner(jid).pause(jid)
    async def close(self):
        import asyncio
        await asyncio.gather(self.public.close(),self.native.close())
