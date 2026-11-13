<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { getJSON } from '../api'

const overview = ref(null)
const rows = ref([])
const streams = ref([])
const streamId = ref(null)
const decision = ref(null)
const err = ref('')

const REASON_LABELS = {
  policy_reject: '策略拒绝',
  new_key_conflict: '新键冲突',
  old_key_not_live: '旧键无存活行',
  same_key: '新旧键相同',
  missing_new_key: '缺新键',
}
const CHECK_LABELS = {
  batch_vs_trace: '批次计数 ↔ 留痕',
  old_key_retired: '旧键退出存活快照',
  new_key_present: '新键在快照键集合',
}

const quarantined = computed(() => rows.value.filter((r) => r.decision === 'rejected'))

async function refresh() {
  overview.value = await getJSON('/api/key-changes/overview')
  const q = new URLSearchParams()
  if (streamId.value) q.set('stream_id', String(streamId.value))
  if (decision.value) q.set('decision', decision.value)
  rows.value = await getJSON('/api/key-changes?' + q.toString())
}

onMounted(async () => {
  try {
    streams.value = await getJSON('/api/streams')
    await refresh()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

watch([streamId, decision], async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>键变更</h1>
    <p class="muted">
      源侧「实体键变更」事件（op=<span class="mono">rename</span>，旧键 → 新键）的处置留痕与隔离。
      迁移把旧键投影行原子改挂到新键（保留载荷与 applied_seq 血缘），拒绝则不动投影、事件进隔离。
    </p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel" v-if="overview">
      <h3>互证</h3>
      <div class="stat-grid">
        <div class="stat"><div class="stat-num">{{ overview.migrated_total }}</div><div class="stat-label">已迁移</div></div>
        <div class="stat"><div class="stat-num">{{ overview.rejected_total }}</div><div class="stat-label">已拒绝（隔离）</div></div>
        <div class="stat"><div class="stat-num">{{ overview.lineage_rows }}</div><div class="stat-label">快照血缘行</div></div>
        <div class="stat">
          <div class="stat-num"><span class="tag" :class="overview.policy === 'migrate' ? 'ok' : 'warn'">{{ overview.policy }}</span></div>
          <div class="stat-label">当前键变更策略</div>
        </div>
      </div>
      <table style="margin-top:0.75rem">
        <thead><tr><th>校验</th><th>结果</th><th>说明</th></tr></thead>
        <tbody>
          <tr v-for="c in overview.checks" :key="c.key">
            <td>{{ CHECK_LABELS[c.key] || c.key }}</td>
            <td><span class="tag" :class="c.ok ? 'ok' : 'danger'">{{ c.ok ? '通过' : '不符' }}</span></td>
            <td class="muted">{{ c.detail }}</td>
          </tr>
        </tbody>
      </table>
      <p class="muted" style="margin-bottom:0">
        批次累计 迁移 {{ overview.batch_applied_rename_sum }} / 拒绝 {{ overview.batch_rejected_rename_sum }}，
        与留痕总数逐条互证；迁移成功的旧键不得再出现在存活快照（双键并存即不符）。
      </p>
    </div>

    <div class="panel" style="display:flex;flex-wrap:wrap;gap:0.75rem;align-items:flex-end">
      <label>
        投影流
        <select v-model="streamId" style="display:block;min-width:14rem;margin-top:0.25rem">
          <option :value="null">全部</option>
          <option v-for="s in streams" :key="s.id" :value="s.id">{{ s.title }}</option>
        </select>
      </label>
      <label>
        处置
        <select v-model="decision" style="display:block;min-width:10rem;margin-top:0.25rem">
          <option :value="null">全部</option>
          <option value="migrated">已迁移</option>
          <option value="rejected">已拒绝（隔离）</option>
        </select>
      </label>
      <span class="muted" style="margin-bottom:0.3rem">隔离区 {{ quarantined.length }} 条（当前筛选下）</span>
    </div>

    <div class="panel">
      <table>
        <thead>
          <tr>
            <th>批次</th><th>流</th><th>event_id</th><th>旧键 → 新键</th><th>seq</th>
            <th>处置</th><th>原因</th><th>迁移前 seq / 事件</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.id">
            <td>{{ r.batch_id }}</td>
            <td>{{ r.stream_title }}</td>
            <td class="mono">{{ r.event_id }}</td>
            <td class="mono">{{ r.old_key }} → {{ r.new_key }}</td>
            <td class="mono">{{ r.seq }}</td>
            <td>
              <span class="tag" :class="r.decision === 'migrated' ? 'ok' : 'danger'">
                {{ r.decision === 'migrated' ? '已迁移' : '已拒绝' }}
              </span>
            </td>
            <td><span v-if="r.reason" class="tag warn">{{ REASON_LABELS[r.reason] || r.reason }}</span><span v-else>—</span></td>
            <td class="mono">
              {{ r.prev_applied_seq == null ? '—' : r.prev_applied_seq + ' / ' + r.prev_applied_event_id }}
            </td>
            <td class="mono">{{ r.created_at }}</td>
          </tr>
          <tr v-if="!rows.length"><td colspan="9" class="muted">暂无键变更留痕，先去投影台跑一趟含 rename 事件的流。</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
