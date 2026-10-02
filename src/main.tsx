import React, { useEffect, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import { View, Text } from 'react-native';
import { invoke } from '@tauri-apps/api/core';
import { open, save } from '@tauri-apps/plugin-dialog';
import { Cloud, Folder, FolderPlus, Upload, Download, ArrowRightLeft, Trash2, Image, FileText, Film, Search, RefreshCw, ChevronRight, Plus, Grid2X2, List, Check, X, ExternalLink, ArrowUpRight, HardDrive, ShieldCheck, LogOut, LoaderCircle, Copy, Pencil, Link2, ArrowLeft, Pause, Play } from 'lucide-react';
import { bytes, demo, rpc, type Item, type Job, type Status } from './api';
import './style.css';

type Volume = { id: string; name: string; provider: string; connected: boolean; can_delete: boolean; can_move: boolean; notice: string };
type Quota = { used_bytes: number | null; total_bytes: number | null; free_bytes: number | null };
type Modal = { kind: 'mkdir' | 'rename' | 'delete' | 'copy'; item?: Item };
const stateNames: Record<string, string> = { queued: 'En cola', transferring: 'Transfiriendo', processing: 'Validando en O2', completed: 'Completado', failed: 'Error', cancelled: 'Cancelado', downloading: 'Descargando', uploading: 'Subiendo', verifying: 'Verificando', moving: 'Moviendo', paused: 'En pausa', interrupted: 'Interrumpido' };
const isPhoto = (item: Item) => /\.(jpe?g|png|webp|gif|heic|avif|mp4|mov|webm)$/i.test(item.name);
function Provider({ provider, small = false }: { provider: string; small?: boolean }) {
  return <span className={`provider-mark ${provider.replace(' ', '-')} ${small ? 'small' : ''}`}>{provider === 'o2' ? 'O₂' : provider === 'drive' ? <span className="drive-triangle"/> : provider === 'google photos' ? <Image size={small ? 16 : 24}/> : <Cloud size={small ? 16 : 24}/>}</span>;
}
function FileIcon({ item }: { item: Item }) {
  return <span className={`file-icon ${item.type === 'folder' ? 'folder' : isPhoto(item) ? 'photo' : 'document'}`}>{item.type === 'folder' ? <Folder/> : /\.(mp4|mov|webm)$/i.test(item.name) ? <Film/> : isPhoto(item) ? <Image/> : <FileText/>}</span>;
}

function App() {
  const [volumes, setVolumes] = useState<Volume[]>([]);
  const [volume, setVolume] = useState('o2');
  const [path, setPath] = useState('/');
  const [items, setItems] = useState<Item[]>([]);
  const [status, setStatus] = useState<Status | null>(null);
  const [quota, setQuota] = useState<Quota | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');
  const [query, setQuery] = useState('');
  const [tab, setTab] = useState<'files' | 'photos'>('files');
  const [grid, setGrid] = useState(false);
  const [selected, setSelected] = useState<Item | null>(null);
  const [modal, setModal] = useState<Modal | null>(null);
  const [input, setInput] = useState('');
  const [targetVolume, setTargetVolume] = useState('');
  const [moving, setMoving] = useState(false);
  const [showTransfers, setShowTransfers] = useState(true);
  const [previewImage, setPreviewImage] = useState<string | null>(null);
  const previous = useRef<string>('');
  const previewIds = useRef(new Set<string>());
  const active = volumes.find(v => v.id === volume);

  async function accounts() {
    const result = await rpc<Volume[]>('volumes');
    for (const [provider, name] of [['drive', 'Google Drive'], ['onedrive', 'OneDrive'], ['google photos', 'Google Fotos']]) {
      if (!result.some(v => v.provider === provider)) result.push({ id: 'add:' + provider, provider, name, connected: false, can_delete: false, can_move: false, notice: '' });
    }
    setVolumes(result);
    return result;
  }
  async function refresh() {
    setLoading(true); setError(''); setSelected(null);
    try {
      const [all, current, currentJobs] = await Promise.all([accounts(), rpc<Status>('status'), rpc<Job[]>('jobs')]); setStatus(current); setJobs(currentJobs);
      const account = all.find(v => v.id === volume);
      if (account?.connected && !(volume === 'o2' && current.expired)) {
        const result = await rpc<Item[]>('list', { path, volume });
        setItems(result.sort((a, b) => Number(b.type === 'folder') - Number(a.type === 'folder') || a.name.localeCompare(b.name, 'es')));
        try { setQuota(await rpc<Quota>('quota', { volume })); } catch { setQuota(null); }
      } else { setItems([]); setQuota(null); }
    } catch (e) { setError(String(e)); } finally { setLoading(false); }
  }
  useEffect(() => { void refresh(); }, [path, volume]);
  useEffect(() => {
    let activeEffect = true, polling = false;
    const timer = window.setInterval(async () => {
      if (polling) return; polling = true;
      try {
        const [next, state] = await Promise.all([rpc<Job[]>('jobs'), rpc<Status>('status')]);
        if (!activeEffect) return;
        setJobs(next); setStatus(state);
        const stamp = next.filter(j => j.state === 'completed').map(j => j.id).join(',') + state.configured + state.auth.state;
        if (previous.current && previous.current !== stamp) void refresh();
        previous.current = stamp;
        if (state.auth.error) setError(state.auth.error.message);
        for (const job of next) {
          if (previewIds.current.has(job.id) && ['completed', 'failed', 'cancelled'].includes(job.state)) {
            previewIds.current.delete(job.id);
            if (job.state === 'completed') { const image = await invoke<string | null>('image_preview', { jobId: job.id }); if (image) setPreviewImage(image); else await invoke('open_preview', { jobId: job.id }); }
            else if (job.error) setError(job.error.message);
          }
        }
      } catch (e) { if (previewIds.current.size) setError(String(e)); }
      finally { polling = false; }
    }, 1500);
    return () => { activeEffect = false; window.clearInterval(timer); };
  }, [path, volume]);
  async function action(work: () => Promise<unknown>, success = '') {
    setBusy(true); setError(''); setNotice('');
    try { await work(); if (success) setNotice(success); } catch (e) { setError(String(e)); }
    finally { setBusy(false); }
  }
  function navigate(item: Item) {
    if (item.type === 'folder') { setPath(item.path); setQuery(''); }
    else setSelected(item);
  }
  async function upload() {
    await action(async () => {
      if (demo) throw new Error('Abre CloudDesk en el escritorio para subir archivos.');
      const files = await open({ multiple: true, directory: false, title: 'Subir a ' + (active?.name || 'la nube') });
      if (!files) return;
      for (const local of Array.isArray(files) ? files : [files]) {
        await rpc('upload', { volume, local, remote: path.replace(/\/$/, '') + '/' + local.split('/').pop() });
      }
      setShowTransfers(true);
    });
  }
  async function download(item: Item) {
    await action(async () => {
      if (demo) throw new Error('La descarga está disponible en la aplicación de escritorio.');
      const local = await save({ defaultPath: item.name, title: 'Guardar archivo' });
      if (local) { await rpc('download', { volume, remote: item.path, local }); setShowTransfers(true); }
    });
  }
  async function preview(item: Item) {
    await action(async () => {
      const job = await rpc<Job>('preview', { volume, path: item.path }); previewIds.current.add(job.id); setShowTransfers(true);
    });
  }
  function showModal(kind: Modal['kind'], item?: Item) {
    setInput(kind === 'rename' ? item!.path : kind === 'copy' ? item!.path : '');
    setTargetVolume(volumes.find(v => v.connected && v.id !== volume)?.id || volume);
    setMoving(false); setModal({ kind, item });
  }
  async function submit(e: React.FormEvent) {
    e.preventDefault(); if (!modal) return;
    await action(async () => {
      if (modal.kind === 'mkdir') {
        if (!input.trim() || /[\/\\]/.test(input) || input === '.' || input === '..') throw new Error('Escribe un nombre de carpeta sin barras.');
        await rpc('mkdir', { volume, path: path.replace(/\/$/, '') + '/' + input.trim() });
      } else if (modal.kind === 'rename') await rpc('move', { volume, path: modal.item!.path, destination: input });
      else if (modal.kind === 'delete') await rpc('delete', { volume, path: modal.item!.path });
      else { await rpc('copy', { source_volume: volume, source: modal.item!.path, destination_volume: targetVolume, destination: input, move: moving }); setShowTransfers(true); }
      setModal(null); await refresh();
    }, modal.kind === 'delete' ? 'Enviado a la papelera.' : 'Operación enviada.');
  }
  const visible = items.filter(i => i.name.toLocaleLowerCase().includes(query.toLocaleLowerCase()) && (tab === 'files' || i.type === 'folder' || isPhoto(i)));
  const crumbs = path.split('/').filter(Boolean);
  const connecting = status?.auth.state === 'connecting';
  const connected = active?.connected && !(volume === 'o2' && status?.expired);
  const used = quota?.used_bytes || 0, total = quota?.total_bytes || 0;
  const running = jobs.filter(j => !['completed', 'failed', 'cancelled', 'interrupted'].includes(j.state));

  return <div className="app-shell min-h-screen dark">
    <aside className="sidebar">
      <div className="brand"><span className="brand-icon"><Cloud size={22}/></span><span>CloudDesk<small>TU ESPACIO EN LA NUBE</small></span></div>
      <div className="sidebar-section">BIBLIOTECA</div>
      <button className={`nav-item ${tab === 'files' ? 'active' : ''}`} onClick={() => setTab('files')}><Folder size={18}/>Archivos<span className="nav-count">{items.length || '—'}</span></button>
      <button className={`nav-item ${tab === 'photos' ? 'active' : ''}`} onClick={() => { setTab('photos'); setGrid(true); }}><Image size={18}/>Fotos y vídeos</button>
      <button className="nav-item" onClick={() => setShowTransfers(!showTransfers)}><ArrowRightLeft size={18}/>Transferencias{running.length > 0 && <span className="nav-count">{running.length}</span>}</button>
      <div className="sidebar-section accounts-heading">TUS CUENTAS<button aria-label="Añadir cuenta" onClick={() => void action(() => rpc('configure'), 'Se ha abierto el asistente de cuentas.')}><Plus size={16}/></button></div>
      {volumes.map(v => <button key={v.id} aria-label={v.name} className={`account-item ${volume === v.id ? 'chosen' : ''}`} onClick={() => { setVolume(v.id); setPath('/'); setQuery(''); }}><Provider provider={v.provider} small/><span>{v.name}<small>{v.provider === 'o2' ? 'Tu nube de O2' : v.provider === 'drive' ? 'Google Drive' : v.provider === 'onedrive' ? 'Microsoft OneDrive' : v.provider === 'google photos' ? 'Google Fotos' : v.provider}</small></span><span className={`status-dot ${v.connected ? 'online' : ''}`}/></button>)}
      <button className="add-account" onClick={() => void action(() => rpc('configure'), 'Se ha abierto el asistente de cuentas.')}><Plus size={16}/>Añadir otra nube</button>
      <div className="sidebar-bottom"><div className="agents-badge"><Link2 size={18}/><div>También para tus agentes<small>Un servicio, todas tus herramientas</small></div></div><div className="local-status"><span className="status-dot online"/>Servicio local<span>v0.1</span></div></div>
    </aside>
    <main>
      <header className="topbar"><View style={{ flexDirection: 'row', alignItems: 'center', gap: 8 }}><HardDrive size={15} color="#9ba9bc"/><Text style={{ color: '#9ba9bc', fontSize: 11 }}>Espacio personal</Text></View><div className="topbar-right">{demo && <span className="demo-tag">Vista de demostración</span>}<span className="secure-label"><ShieldCheck size={15}/>Credenciales en tu llavero</span><span className="avatar">C</span></div></header>
      <div className="main-content">
        <div className="page-heading"><div><div className="eyebrow">TODO EN SU SITIO</div><h1>{tab === 'photos' ? 'Tus recuerdos, organizados.' : 'Tus archivos, a mano.'}</h1><p>Explora tus nubes y mueve lo que necesites entre ellas.</p></div><button className="button primary" onClick={() => void upload()} disabled={busy || !connected}><Upload size={17}/>Subir archivos</button></div>
        <section className="account-overview" aria-label="Cuenta seleccionada"><div className="overview-main"><Provider provider={active?.provider || 'o2'}/><div><h2>{active?.name || 'O2 Cloud'}</h2><p><span className={`status-dot ${connected ? 'online' : ''}`}/>{connected ? 'Cuenta conectada' : connecting ? 'Completa el acceso en el navegador' : 'Pendiente de conectar'}</p></div></div><div className="storage-summary"><div><span>{quota ? bytes(quota.used_bytes) : '—'} utilizados</span><span>{total ? bytes(total) + ' en total' : 'Cuota no disponible'}</span></div><div className="storage-track"><span style={{ width: total ? Math.max(1, Math.min(100, used / total * 100)) + '%' : '0%' }}/></div><small>{quota?.free_bytes != null ? bytes(quota.free_bytes) + ' disponibles' : 'Tu cuenta mantiene sus permisos habituales'}</small></div><div className="account-actions">{volume === 'o2' && <button className="button secondary" onClick={() => void action(() => rpc('login'))} disabled={connecting || busy}>{connecting ? <LoaderCircle size={16} className="spin"/> : <Link2 size={16}/>} {connecting ? 'Conectando…' : connected ? 'Comprobar sesión' : 'Conectar O2'}</button>}<button className="icon-button" aria-label="Actualizar cuentas" onClick={() => void refresh()}><RefreshCw size={17}/></button>{volume === 'o2' && connected && <button className="icon-button" aria-label="Desconectar O2" onClick={() => void action(async () => { await rpc('logout'); await refresh(); })}><LogOut size={17}/></button>}</div></section>
        {active?.notice && <div className="provider-notice"><Image size={17}/>{active.notice}</div>}
        {error && <div className="message error" role="alert"><span>{error}</span><button aria-label="Cerrar error" onClick={() => setError('')}><X size={17}/></button></div>}
        {notice && <div className="message success" role="status"><Check size={17}/><span>{notice}</span><button aria-label="Cerrar aviso" onClick={() => setNotice('')}><X size={17}/></button></div>}
        <section className="files-panel"><div className="files-toolbar"><nav className="breadcrumbs" aria-label="Ruta actual"><button onClick={() => setPath('/')}><Folder size={16}/>{active?.name || 'O2 Cloud'}</button>{crumbs.map((part, index) => <React.Fragment key={index}><ChevronRight size={14}/><button onClick={() => setPath('/' + crumbs.slice(0, index + 1).join('/'))}>{part}</button></React.Fragment>)}</nav><div className="toolbar-actions"><button className="button compact secondary" disabled={!connected || busy} onClick={() => showModal('mkdir')}><FolderPlus size={16}/>Nueva carpeta</button><div className="view-switch"><button className={!grid ? 'selected' : ''} aria-label="Vista de lista" onClick={() => setGrid(false)}><List size={17}/></button><button className={grid ? 'selected' : ''} aria-label="Vista de cuadrícula" onClick={() => setGrid(true)}><Grid2X2 size={16}/></button></div></div></div>
          <div className="filterbar"><div className="filter-tabs"><button className={tab === 'files' ? 'selected' : ''} onClick={() => setTab('files')}>Todo<span>{items.length}</span></button><button className={tab === 'photos' ? 'selected' : ''} onClick={() => { setTab('photos'); setGrid(true); }}>Fotos y vídeos<span>{items.filter(isPhoto).length}</span></button></div><label className="search"><Search size={16}/><input aria-label="Buscar en esta carpeta" placeholder="Buscar en esta carpeta" value={query} onChange={e => setQuery(e.target.value)}/></label></div>
          {loading ? <div className="empty-state"><LoaderCircle className="spin"/><h3>Cargando tus archivos…</h3></div> : !connected ? <div className="empty-state"><span className="empty-icon"><Cloud size={35}/></span><h3>Tu nube empieza aquí</h3><p>Conecta O2 o añade una cuenta de Drive, OneDrive o Google Fotos.</p><button className="button primary" onClick={() => void action(() => volume === 'o2' ? rpc('login') : rpc('configure'))} disabled={connecting}>Conectar cuenta<ArrowUpRight size={16}/></button></div> : visible.length === 0 ? <div className="empty-state"><span className="empty-icon"><Folder size={32}/></span><h3>{query ? 'No hay coincidencias' : 'Un espacio para lo que viene'}</h3><p>{tab === 'photos' ? 'No hay fotos ni vídeos en esta carpeta.' : 'Sube archivos o crea tu primera carpeta.'}</p>{path !== '/' && <button className="button secondary" onClick={() => setPath('/' + crumbs.slice(0, -1).join('/'))}><ArrowLeft size={16}/>Volver</button>}</div> : grid ? <div className="file-grid">{visible.map(item => <button key={item.path} className={`file-card ${selected?.path === item.path ? 'is-selected' : ''}`} onClick={() => navigate(item)} onDoubleClick={() => item.type === 'file' && void preview(item)}><FileIcon item={item}/><span>{item.name}</span><small>{item.type === 'folder' ? 'Carpeta' : bytes(item.size)}</small></button>)}</div> : <div className="table-wrap"><table><thead><tr><th>Nombre</th><th>Tamaño</th><th>Modificado</th><th><span className="sr-only">Acciones</span></th></tr></thead><tbody>{visible.map(item => <tr key={item.path} className={selected?.path === item.path ? 'is-selected' : ''}><td><button className="file-name" onClick={() => navigate(item)} onDoubleClick={() => item.type === 'file' && void preview(item)}><FileIcon item={item}/><span>{item.name}<small>{item.type === 'folder' ? 'Carpeta' : item.name.split('.').pop()?.toUpperCase()}</small></span></button></td><td>{bytes(item.size)}</td><td>{item.modified ? new Date(item.modified).toLocaleDateString('es', { day: 'numeric', month: 'short', year: 'numeric' }) : '—'}</td><td><button className="row-action" aria-label={'Copiar ' + item.name + ' a otra nube'} onClick={() => showModal('copy', item)}><ArrowRightLeft size={16}/></button><button className="row-action" aria-label={'Ver detalles de ' + item.name} onClick={() => setSelected(item)}><ChevronRight size={17}/></button></td></tr>)}</tbody></table></div>}
          <div className="files-footer"><span>{visible.length} elementos{tab === 'photos' ? ' · fotos, vídeos y carpetas' : ''}</span><span><ShieldCheck size={13}/>Acceso seguro desde tu escritorio</span></div>
        </section>
        {selected && <section className="selection-panel"><FileIcon item={selected}/><div className="selection-name"><strong>{selected.name}</strong><small>{selected.path} · {bytes(selected.size)}</small></div><div className="selection-actions">{selected.type === 'file' && <><button className="button secondary compact" onClick={() => void preview(selected)} disabled={busy}><ExternalLink size={15}/>Ver</button><button className="button secondary compact" onClick={() => void download(selected)} disabled={busy}><Download size={15}/>Descargar</button></>}<button className="button secondary compact" onClick={() => showModal('copy', selected)} disabled={busy}><Copy size={15}/>Otra nube</button>{active?.can_move && <button className="icon-button" aria-label="Mover o renombrar" onClick={() => showModal('rename', selected)}><Pencil size={16}/></button>}{active?.can_delete && <button className="icon-button danger" aria-label="Enviar a la papelera" onClick={() => showModal('delete', selected)}><Trash2 size={16}/></button>}<button className="icon-button" aria-label="Cerrar selección" onClick={() => setSelected(null)}><X size={17}/></button></div></section>}
        {showTransfers && <section className="transfers-panel"><div className="transfers-title"><h2><ArrowRightLeft size={17}/>Transferencias<span>{running.length ? running.length + ' activas' : 'Todo al día'}</span></h2><button aria-label="Ocultar transferencias" onClick={() => setShowTransfers(false)}><X size={17}/></button></div>{jobs.length === 0 ? <div className="transfers-empty"><span className="transfer-check"><Check size={16}/></span><div>Todo listo para tu próxima transferencia<small>Las subidas, descargas y movimientos aparecerán aquí.</small></div></div> : <div className="jobs-list">{jobs.slice(-6).reverse().map(job => <div className="job" key={job.id}><span className={`job-icon ${job.state === 'failed' ? 'danger' : ''}`}>{job.state === 'completed' ? <Check size={18}/> : job.kind === 'upload' ? <Upload size={18}/> : <ArrowRightLeft size={18}/>}</span><div className="job-content"><div><strong>{job.remote.split('/').pop()}</strong><span>{stateNames[job.state] || job.state}</span></div><div className="job-track"><span style={{ width: job.progress + '%' }}/></div><small>{job.error?.message || `${bytes(job.bytes)} de ${bytes(job.total)} · ${job.progress}% · ${bytes(job.speed || 0)}/s · ${job.state === 'completed' ? 'Finalizado en ' + job.elapsed + ' s' : job.state === 'paused' ? 'En pausa' : job.eta != null ? '~' + Math.ceil(job.eta / 60) + ' min restantes' : 'Calculando tiempo…'}`}</small></div>{job.state === 'paused' || (job.resumable && job.partial && ['failed', 'cancelled', 'interrupted'].includes(job.state)) ? <button className="icon-button" aria-label="Reanudar transferencia" onClick={() => void action(() => rpc('resume', { id: job.id }))}><Play size={16}/></button> : job.can_pause && !['completed', 'failed', 'cancelled', 'interrupted'].includes(job.state) ? <button className="icon-button" aria-label="Pausar transferencia" onClick={() => void action(() => rpc('pause', { id: job.id }))}><Pause size={16}/></button> : null}{!['completed', 'failed', 'cancelled', 'interrupted'].includes(job.state) && <button className="icon-button" aria-label="Cancelar transferencia" onClick={() => void action(() => rpc('cancel', { id: job.id }))}><X size={16}/></button>}</div>)}</div>}</section>}
        <footer className="page-footer">Hecho para tus archivos. Y para lo que haces con ellos.<span>CloudDesk · Local & privado</span></footer>
      </div>
    </main>
    {previewImage && <div className="modal-backdrop" onClick={() => setPreviewImage(null)}><div className="image-modal" onClick={e => e.stopPropagation()}><button className="icon-button" aria-label="Cerrar vista previa" onClick={() => setPreviewImage(null)}><X size={23}/></button><img src={previewImage} alt="Vista previa del archivo seleccionado"/></div></div>}
    {modal && <div className="modal-backdrop" onClick={() => !busy && setModal(null)}><form className="modal" onClick={e => e.stopPropagation()} onSubmit={submit}><div className="modal-heading"><span className="empty-icon">{modal.kind === 'mkdir' ? <FolderPlus/> : modal.kind === 'delete' ? <Trash2/> : <ArrowRightLeft/>}</span><button type="button" aria-label="Cerrar diálogo" disabled={busy} onClick={() => setModal(null)}><X size={20}/></button></div><h2>{modal.kind === 'mkdir' ? 'Una nueva carpeta' : modal.kind === 'rename' ? 'Mover o renombrar' : modal.kind === 'delete' ? 'Enviar a la papelera' : 'De una nube a otra'}</h2><p>{modal.kind === 'delete' ? `«${modal.item!.name}» se enviará a la papelera de ${active?.name}.` : modal.kind === 'mkdir' ? 'Dale un nombre a tu nuevo espacio.' : modal.kind === 'rename' ? 'Escribe la ruta completa de destino. El nombre final determina el nuevo nombre del archivo.' : `Origen: ${active?.name} · ${modal.item!.path}`}</p>{modal.kind === 'copy' && <><label>Cuenta de destino<select value={targetVolume} onChange={e => setTargetVolume(e.target.value)}>{volumes.filter(v => v.connected).map(v => <option key={v.id} value={v.id}>{v.name}</option>)}</select></label><div className="transfer-choice"><button type="button" className={!moving ? 'chosen' : ''} onClick={() => setMoving(false)}>Copiar<small>Conserva el original</small></button><button type="button" className={moving ? 'chosen' : ''} onClick={() => setMoving(true)} disabled={modal.item?.type === 'folder' || active?.provider === 'google photos' || volumes.find(v => v.id === targetVolume)?.provider === 'google photos'}>Mover<small>Verifica antes de retirar</small></button></div></>}{modal.kind !== 'delete' && <label>{modal.kind === 'mkdir' ? 'Nombre de carpeta' : 'Ruta de destino'}<input autoFocus value={input} onChange={e => setInput(e.target.value)} placeholder={modal.kind === 'mkdir' ? 'Proyectos personales' : '/Carpeta/archivo.ext'} required/></label>}{error && <div className="modal-error" role="alert">{error}</div>}<div className="modal-buttons"><button className="button secondary" type="button" disabled={busy} onClick={() => setModal(null)}>Cancelar</button><button className={`button ${modal.kind === 'delete' ? 'destructive' : 'primary'}`} type="submit" disabled={busy}>{busy ? <LoaderCircle size={16} className="spin"/> : modal.kind === 'delete' ? <Trash2 size={16}/> : <Check size={16}/>} {modal.kind === 'delete' ? 'Enviar a papelera' : modal.kind === 'mkdir' ? 'Crear carpeta' : modal.kind === 'copy' ? moving ? 'Mover archivo' : 'Copiar' : 'Guardar cambio'}</button></div></form></div>}
  </div>;
}

createRoot(document.getElementById('root')!).render(<App/>);
