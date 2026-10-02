import { invoke, isTauri } from '@tauri-apps/api/core';

export type Item = { id: string; name: string; type: 'file' | 'folder'; path: string; size: number | null; modified: string | null; content_type: string | null };
export type Job = { id: string; kind: 'upload' | 'download' | 'copy' | 'move'; remote: string; local: string; state: string; bytes: number; total: number; progress: number; speed: number; eta: number | null; elapsed: number; can_pause: boolean; resumable: boolean; partial?: string; error: { code: string; message: string } | null; result: unknown };
export type Status = { configured: boolean; expired: boolean; auth: { state: string; error: { message: string } | null }; backend: string };
export const demo = !isTauri();
let demoItems: Item[] = [
  { id: '1', name: 'Proyectos', type: 'folder', path: '/Proyectos', size: null, modified: '2026-10-01T10:00:00Z', content_type: null },
  { id: '2', name: 'Fotografía', type: 'folder', path: '/Fotografía', size: null, modified: '2026-09-29T10:00:00Z', content_type: null },
  { id: '3', name: 'Notas de trabajo.md', type: 'file', path: '/Notas de trabajo.md', size: 8400, modified: '2026-10-01T10:00:00Z', content_type: 'text/markdown' },
  { id: '4', name: 'Ideas para el otoño.pdf', type: 'file', path: '/Ideas para el otoño.pdf', size: 2100000, modified: '2026-09-28T10:00:00Z', content_type: 'application/pdf' },
];

const demoJobs: Job[] = [{ id: 'demo-transfer', kind: 'download', remote: '/Ejemplo de transferencia.zip', local: '', state: 'paused', bytes: 24000000, total: 80000000, progress: 30, speed: 1200000, eta: 47, elapsed: 20, can_pause: true, resumable: true, error: null, result: null }];

// Browser preview is explicitly a demo and never accesses the authenticated bridge.
async function demoRpc(operation: string, args: Record<string, unknown>): Promise<unknown> {
  if (operation === 'volumes') return [
    {id:'o2',name:'O2 Cloud',provider:'o2',connected:true,can_delete:true,can_move:true,notice:''},
    {id:'rclone:drive',name:'Google Drive',provider:'drive',connected:true,can_delete:true,can_move:true,notice:''},
    {id:'rclone:onedrive',name:'OneDrive',provider:'onedrive',connected:true,can_delete:true,can_move:true,notice:''},
    {id:'rclone:fotos',name:'Google Fotos',provider:'google photos',connected:true,can_delete:false,can_move:false,notice:'Solo contenido creado mediante esta integración. Google Fotos no permite borrar fotos desde su API.'}
  ];
  if (operation === 'status') return { configured: true, expired: false, auth: { state: 'connected', error: null }, backend: 'demo' };
  if (operation === 'quota') return { used_bytes: 24400000, total_bytes: 10000000000000, free_bytes: 9999975600000 };
  if (operation === 'jobs') return demoJobs;
  if (operation === 'pause' || operation === 'resume') { const job = demoJobs.find(j => j.id === args.id)!; job.state = operation === 'pause' ? 'paused' : 'transferring'; return job; }
  if (operation === 'list') return demoItems.filter(i => i.path.substring(0, i.path.lastIndexOf('/')) === (args.path === '/' ? '' : args.path));
  if (operation === 'mkdir') {
    const item: Item = { id: String(Date.now()), name: String(args.path).split('/').pop()!, path: String(args.path), type: 'folder', size: null, modified: new Date().toISOString(), content_type: null };
    demoItems = [...demoItems, item]; return item;
  }
  if (operation === 'move') {
    demoItems = demoItems.map(i => i.path === args.path ? { ...i, path: String(args.destination), name: String(args.destination).split('/').pop()! } : i); return {};
  }
  if (operation === 'delete') { demoItems = demoItems.filter(i => i.path !== args.path); return { trashed: true }; }
  if (operation === 'login') return { state: 'connected', error: null };
  throw new Error('Esta acción requiere la aplicación de escritorio. La vista del navegador es una demostración.');
}

export async function rpc<T>(operation: string, args: Record<string, unknown> = {}): Promise<T> {
  if (demo) return await demoRpc(operation, args) as T;
  return invoke<T>('bridge_rpc', { operation, args });
}

export function bytes(value: number | null): string {
  if (value === null) return '—';
  if (!value) return '0 B';
  const unit = Math.min(Math.floor(Math.log(value) / Math.log(1000)), 4);
  return `${(value / 1000 ** unit).toLocaleString('es', { maximumFractionDigits: 1 })} ${['B', 'KB', 'MB', 'GB', 'TB'][unit]}`;
}
