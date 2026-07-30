import { useState, useEffect } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Save, Settings2, CheckCircle2, AlertCircle, KeyRound, User, Shield } from 'lucide-react'
import api from '../api/client'
import { useAuth } from '../contexts/AuthContext'

function Field({ label, hint, children }) {
  return (
    <div>
      <label className="text-xs font-medium text-gray-300 block mb-1.5">{label}</label>
      {children}
      {hint && <p className="text-xs text-gray-500 mt-1">{hint}</p>}
    </div>
  )
}

function Section({ title, icon: Icon, children }) {
  return (
    <div className="rounded-xl border border-gray-700 bg-gray-800/40 overflow-hidden">
      <div className="px-5 py-3 border-b border-gray-700 flex items-center gap-2">
        <Icon size={15} className="text-sky-400" />
        <span className="text-sm font-semibold text-gray-200">{title}</span>
      </div>
      <div className="p-5 space-y-4">{children}</div>
    </div>
  )
}

export default function Settings() {
  const { user } = useAuth()
  const qc = useQueryClient()

  const { data: appSettings, isLoading } = useQuery({
    queryKey: ['settings'],
    queryFn: () => api.get('/settings/').then(r => r.data),
  })

  const [form, setForm] = useState({
    download_base_name: '',
    download_version: '',
    recon_cron_time: '',
    dump_ingest_times: '',
  })

  useEffect(() => {
    if (appSettings) {
      setForm(p => ({
        download_base_name: appSettings.download_base_name ?? p.download_base_name,
        download_version: appSettings.download_version ?? p.download_version,
        recon_cron_time: appSettings.recon_cron_time ?? p.recon_cron_time,
        dump_ingest_times: appSettings.dump_ingest_times ?? p.dump_ingest_times,
      }))
    }
  }, [appSettings])

  const scope = appSettings?.scope_config

  const save = useMutation({
    mutationFn: () => api.patch('/settings/', form),
    onSuccess: () => qc.invalidateQueries(['settings']),
  })

  const isAdmin = user?.role === 'admin'

  const inputCls = `w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-gray-200 font-mono
    focus:outline-none focus:border-sky-500 disabled:opacity-40 disabled:cursor-not-allowed`

  return (
    <div className="max-w-2xl space-y-6 pb-10">
      <div>
        <h1 className="text-xl font-bold text-white">Settings</h1>
        <p className="text-sm text-gray-400 mt-1">Application configuration. Admin-only fields are read-only for operators.</p>
      </div>

      {/* Current user info */}
      <Section title="Your Account" icon={User}>
        <div className="flex items-center gap-3 bg-gray-700/30 rounded-lg px-4 py-3">
          <div className="w-9 h-9 rounded-full bg-sky-600/20 border border-sky-600/40 flex items-center justify-center text-sky-300 font-bold text-sm">
            {user?.username?.[0]?.toUpperCase()}
          </div>
          <div>
            <p className="text-sm font-medium text-white">{user?.username}</p>
            <p className="text-xs text-gray-400">{user?.email ?? 'No email set'}</p>
          </div>
          <div className="ml-auto flex items-center gap-1.5 px-2 py-0.5 rounded-full border border-sky-600/40 bg-sky-600/10">
            <Shield size={11} className="text-sky-400" />
            <span className="text-xs font-mono text-sky-300">{user?.role}</span>
          </div>
        </div>
      </Section>

      {/* App settings */}
      <Section title="Application Settings" icon={Settings2}>
        {isLoading ? (
          <p className="text-xs text-gray-500">Loading…</p>
        ) : (
          <>
            <div className="grid grid-cols-2 gap-4">
              <Field label="Download Base Name" hint="Prefix for exported file names">
                <input
                  value={form.download_base_name}
                  onChange={e => setForm(p => ({ ...p, download_base_name: e.target.value }))}
                  disabled={!isAdmin}
                  className={inputCls}
                />
              </Field>
              <Field label="Download Version" hint="Version tag in exported file names">
                <input
                  value={form.download_version}
                  onChange={e => setForm(p => ({ ...p, download_version: e.target.value }))}
                  disabled={!isAdmin}
                  className={inputCls}
                />
              </Field>
            </div>

            <Field
              label="Reconciliation Cron Time (HH:MM IST)"
              hint="Nightly automatic reconciliation time. Requires worker restart to take effect."
            >
              <input
                value={form.recon_cron_time}
                onChange={e => setForm(p => ({ ...p, recon_cron_time: e.target.value }))}
                placeholder="02:00"
                disabled={!isAdmin}
                className={inputCls}
              />
            </Field>

            <Field
              label="Dump Ingest Times (comma-separated HH:MM IST)"
              hint="Scheduled times to auto-scan for new dump files in the incoming directory."
            >
              <input
                value={form.dump_ingest_times}
                onChange={e => setForm(p => ({ ...p, dump_ingest_times: e.target.value }))}
                placeholder="06:00,18:00"
                disabled={!isAdmin}
                className={inputCls}
              />
            </Field>

            {isAdmin && (
              <div className="flex items-center gap-3 pt-2">
                <button
                  onClick={() => save.mutate()}
                  disabled={save.isPending}
                  className="flex items-center gap-2 px-4 py-2 rounded-lg bg-sky-600 hover:bg-sky-500 disabled:opacity-40 text-white text-sm font-semibold transition-all"
                >
                  <Save size={14} />
                  {save.isPending ? 'Saving…' : 'Save Changes'}
                </button>
                {save.isSuccess && (
                  <span className="flex items-center gap-1.5 text-green-400 text-xs">
                    <CheckCircle2 size={14} /> Saved
                  </span>
                )}
                {save.isError && (
                  <span className="flex items-center gap-1.5 text-red-400 text-xs">
                    <AlertCircle size={14} /> Error saving
                  </span>
                )}
              </div>
            )}

            {!isAdmin && (
              <p className="text-xs text-gray-500 italic pt-1">Only administrators can modify these settings.</p>
            )}
          </>
        )}
      </Section>

      {/* Engine scope configuration — live values from backend .env (read-only) */}
      <Section title="Engine Scope Configuration (.env — read-only)" icon={KeyRound}>
        {!scope ? (
          <p className="text-xs text-gray-500">Loading…</p>
        ) : (
          <>
            <div className="space-y-2 text-xs font-mono">
              {[
                ['PRR Scope Prefixes (name starts with)', scope.prr_scope_prefixes],
                ['PRR Scope Suffixes (name ends with)', scope.prr_scope_suffixes],
                ['PRR Rule Name Max Length', String(scope.prr_name_max_len)],
              ].map(([k, v]) => (
                <div key={k} className="flex justify-between gap-4 py-1.5 border-b border-gray-700/40">
                  <span className="text-gray-400">{k}</span>
                  <span className="text-sky-300">{v}</span>
                </div>
              ))}
            </div>

            <div>
              <p className="text-xs font-medium text-gray-300 mb-2">
                RBAR Scope Rules (destination prefix + suffix, per DRA category)
              </p>
              <table className="w-full text-xs font-mono">
                <thead>
                  <tr className="text-gray-400 border-b border-gray-700">
                    <th className="text-left py-1.5 font-medium">Category</th>
                    <th className="text-left py-1.5 font-medium">Prefixes</th>
                    <th className="text-left py-1.5 font-medium">Suffixes</th>
                  </tr>
                </thead>
                <tbody>
                  {Object.entries(scope.rbar_scope_rules ?? {}).map(([cat, rule]) => (
                    <tr key={cat} className="border-b border-gray-700/40">
                      <td className="py-1.5 text-amber-300 capitalize">{cat}</td>
                      <td className="py-1.5 text-sky-300">{(rule.prefixes ?? []).join(', ') || '—'}</td>
                      <td className="py-1.5 text-sky-300">{(rule.suffixes ?? []).join(', ') || '—'}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <p className="text-xs text-gray-500 mt-2">
                "default" applies to every category without its own row (e.g. Core, IoT, Charging).
                Values come from PRR_SCOPE_PREFIXES / PRR_SCOPE_SUFFIXES / RBAR_SCOPE_RULES in .env —
                changing them requires an api + worker container restart, after which this page reflects
                the new values automatically.
              </p>
            </div>

            <div className="flex justify-between gap-4 py-1.5 text-xs font-mono">
              <span className="text-gray-400">All timestamps in IST</span>
              <span className="text-sky-300">Asia/Kolkata</span>
            </div>
          </>
        )}
      </Section>
    </div>
  )
}
