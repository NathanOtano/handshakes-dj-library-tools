'use strict';
const el = id => document.getElementById(id);
const session = document.querySelector('meta[name="dj-session"]').content;
let state = {reports: {}}, pending = false, alive = true;
const number = n => Number(n || 0).toLocaleString('fr-FR');
const size = n => {const bytes=Number(n||0), unit=Math.min(3,Math.max(0,Math.floor(Math.log(Math.max(1,bytes))/Math.log(1024))));return (bytes/1024**unit).toLocaleString('fr-FR',{maximumFractionDigits:1})+' '+['o','Ko','Mo','Go'][unit];};
const issuesFR = {empty_usb_selection:'Aucun morceau sélectionné pour la clé.', unknown_selected_ids:'Certains morceaux sélectionnés sont inconnus.', selected_tracks_without_bucket:'Des morceaux n’ont pas encore de DJ bac.', derived_buckets_need_refresh:'Les bacs doivent être actualisés.', selected_audio_missing:'Des fichiers audio sont introuvables.', playlist_above_1000:'Une playlist dépasse 1 000 morceaux.', usb_elements_above_limit:'La sélection USB dépasse 1 000 éléments.'};
const phasesFR = {backup:'Sauvegarde', audio:'Copie des morceaux', metadata:'Copie des analyses', install_databases:'Installation des catalogues', readback:'Relecture de la clé', complete:'Terminé'};
function show(view) {
  document.querySelectorAll('.view').forEach(p => {p.hidden = p.id !== view;});
  document.querySelectorAll('nav button').forEach(b => {if (b.dataset.view === view) b.setAttribute('aria-current', 'page'); else b.removeAttribute('aria-current');});
}
function node(tag, text, cls) {const n = document.createElement(tag); n.textContent = text; if (cls) n.className = cls; return n;}
function metric(value, label) {const n = node('div', '', 'metric'); n.append(node('strong', number(value)), node('span', label)); return n;}
function changes(data) {
  const ul = node('ul', '', 'change-list');
  for (const [key, label] of [['new', 'Morceaux à ajouter'], ['replace', 'Morceaux à remplacer'], ['reuse', 'Morceaux conservés']]) {
    const li = node('li', ''); li.append(node('span', label), node('strong', number(data.counts[key]))); ul.append(li);
  }
  return ul;
}
function render() {
  const r = state.reports, audit = r.audit, plan = r.prepare;
  el('library-name').textContent = state.libraryLabel || 'Bibliothèque';
  el('target-name').textContent = state.targetLabel || 'Clé USB';
  el('message').textContent = state.error || '';
  el('library-metrics').replaceChildren(); el('issues').replaceChildren();
  if (audit) {
    el('library-metrics').append(metric(audit.tracks, 'Morceaux'), metric(audit.unbucketedCount ?? audit.unbucketed ?? 0, 'Sans bac'), metric(audit.elements ?? ((audit.playlists || 0) + (audit.folders || 0)), 'Éléments USB / 1 000'));
    for (const issue of (audit.errors || [])) {const code=String(issue), message=issuesFR[code] || (code.startsWith('usb_bucket_membership_mismatch:') ? 'La sélection d’un bac doit être actualisée.' : 'Un contrôle n’est pas validé. Consulter les détails.');const li=node('li',message);li.title=code;el('issues').append(li);}
  }
  el('audit-status').textContent = audit ? (!state.auditFresh ? 'Dernier contrôle' : audit.status === 'pass' ? 'Vérifiée' : 'À corriger') : 'À vérifier';
  el('prepare-status').textContent = state.planToken ? 'Prêt à confirmer' : 'Non préparé';
  el('prepare-content').replaceChildren(); el('usb-content').replaceChildren();
  if (plan) {
    el('prepare-content').append(changes(plan), node('p', 'Sauvegarde nécessaire : ' + size(plan.backup_bytes), 'target-details'));
    el('usb-content').append(changes(plan));
    const i = plan.identity;
    el('usb-content').append(node('p', 'Clé ' + i.label + ' · ' + i.fs + ' · ' + size(i.free) + ' disponibles', 'target-details'), node('p', 'Série : ' + i.serial, 'target-details'));
  } else {
    el('prepare-content').append(node('p', audit?.status === 'pass' ? 'La bibliothèque est vérifiée. Calculer les changements vers la clé.' : 'Vérifier la bibliothèque avant de préparer la clé.'));
    el('usb-content').append(node('p', 'Préparer l’aperçu pour identifier la clé et ses changements.'));
  }
  el('sync-status').textContent = r.sync?.status === 'pass' ? 'Copie vérifiée' : 'En attente';
  el('audit').disabled = state.busy || pending || !state.configured?.audit;
  el('prepare-action').disabled = state.busy || pending || !state.auditFresh || audit?.status !== 'pass' || !state.configured?.prepare;
  el('sync').disabled = state.busy || pending || !state.planToken || !state.configured?.sync;
  el('shutdown').disabled = state.busy || pending;
  el('progress').hidden = !state.busy;
  el('phase').textContent = phasesFR[state.progress?.phase] || state.phase || '';
  const progress = document.querySelector('progress');
  if (state.progress?.total_files) {progress.max = state.progress.total_files; progress.value = state.progress.completed_files || 0;} else progress.removeAttribute('value');
}
async function refresh() {
  if (!alive) return;
  try {const r = await fetch('/api/state'); if (!r.ok) throw Error(); state = await r.json(); render();
    if (el('details').open) {const log = await fetch('/api/log').then(r => r.json()); el('log').textContent = log.text;}
  } catch {el('message').textContent = 'Interface arrêtée ou connexion locale indisponible.'; document.querySelectorAll('.primary').forEach(b => b.disabled = true);}
}
async function action(path, body) {
  if (pending) return;
  pending = true; render();
  try {const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-DJ-Token': session}, body: JSON.stringify(body)}); const data = await r.json(); if (!r.ok) throw Error(data.error); state = data;
    if (path === '/api/shutdown') {alive = false; el('message').textContent = 'Interface arrêtée. Vous pouvez fermer cet onglet.';}
  } catch (error) {state.error = error.message;}
  finally {pending = false; if (alive) render();}
}
document.querySelectorAll('nav button').forEach(b => b.addEventListener('click', () => show(b.dataset.view)));
el('audit').onclick = () => action('/api/start', {task: 'audit'});
el('prepare-action').onclick = () => action('/api/start', {task: 'prepare'});
el('sync').onclick = () => {el('confirm-target').textContent = 'Destination : ' + state.targetLabel + ' · série ' + state.reports.prepare.identity.serial; el('confirm').showModal();};
el('cancel').onclick = () => el('confirm').close();
el('confirm-copy').onclick = () => {el('confirm').close(); action('/api/start', {task: 'sync', confirmation: state.planToken});};
el('stop').onclick = () => action('/api/stop', {});
el('shutdown').onclick = () => action('/api/shutdown', {});
el('help').onclick = () => el('help-dialog').showModal(); el('close-help').onclick = () => el('help-dialog').close();
el('details').ontoggle = () => {if (el('details').open) refresh();};
refresh(); setInterval(refresh, 800);
