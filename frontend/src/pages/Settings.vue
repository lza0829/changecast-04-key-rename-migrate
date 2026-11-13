<script setup>
import { onMounted, ref } from 'vue'
import { getJSON, putJSON } from '../api'

const settings = ref({})
const policy = ref('migrate')
const busy = ref(false)
const err = ref('')
const saved = ref(false)

const READONLY_KEYS = ['apply_order', 'duplicate_policy', 'tombstone_mode']

async function refresh() {
  settings.value = await getJSON('/api/settings')
  policy.value = settings.value.key_change_policy || 'migrate'
}

async function save() {
  err.value = ''
  saved.value = false
  busy.value = true
  try {
    settings.value = await putJSON('/api/settings/key_change_policy', { value: policy.value })
    saved.value = true
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

onMounted(async () => {
  try { await refresh() } catch (e) { err.value = String(e.message || e) }
})
</script>
<template>
  <div class="page">
    <h1>设置</h1>
    <p class="muted">键变更策略互斥切换（迁移 / 拒绝迁移，二者居一）；其余底座策略只读展示。</p>
    <p v-if="err" class="tag danger">{{ err }}</p>

    <div class="panel">
      <h3 style="margin-top:0">键变更策略（互斥）</h3>
      <label style="display:block;margin-bottom:0.6rem">
        <input type="radio" value="migrate" v-model="policy" />
        <strong>迁移（migrate）</strong>
        <span class="muted">
          — 旧键投影行原子改挂到新键，保留载荷与 applied_seq 血缘（renamed_from / prev_applied_seq 可溯），
          旧键退出存活快照；新键已有存活行则冲突拒绝，整事件失败、两键状态与开跑前一致。
        </span>
      </label>
      <label style="display:block;margin-bottom:0.6rem">
        <input type="radio" value="reject" v-model="policy" />
        <strong>拒绝迁移（reject）</strong>
        <span class="muted">— 不改投影，键变更事件进隔离留痕，旧键行保持不动。</span>
      </label>
      <p class="muted" style="margin:0 0 0.75rem">
        新键冲突的细化规则（本实现贯彻其一）：<strong>冲突拒绝迁移</strong>——不并入、不覆盖，事件进隔离并留痕。
      </p>
      <button :disabled="busy" @click="save">{{ busy ? '保存中…' : '保存策略' }}</button>
      <span v-if="saved" class="tag ok" style="margin-left:0.6rem">已保存</span>
      <p class="muted" style="margin:0.75rem 0 0">
        切换后到<router-link to="/run">投影台</router-link>重跑投影流生效；处置结果见
        <router-link to="/key-changes">键变更</router-link>页。
      </p>
    </div>

    <div class="panel">
      <h3 style="margin-top:0">底座策略（只读）</h3>
      <table>
        <thead><tr><th>键</th><th>值</th></tr></thead>
        <tbody>
          <tr v-for="k in READONLY_KEYS" :key="k">
            <td class="mono">{{ k }}</td>
            <td><span class="tag">{{ settings[k] }}</span></td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
