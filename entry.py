from workers import Response, WorkerEntrypoint

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = request.url
        if url.endswith('/api/health'):
            return Response.json({'ok': True, 'service': 'botbyte-worker'})
        if url.endswith('/api/db-test'):
            try:
                result = await self.env.DB.prepare(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).all()
                return Response.json({'ok': True, 'tables': result.results})
            except Exception as e:
                return Response.json({'ok': False, 'error': str(e)}, status=500)
        return await self.env.ASSETS.fetch(request)
