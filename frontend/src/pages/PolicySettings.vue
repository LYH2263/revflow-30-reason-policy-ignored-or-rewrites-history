<script setup>
import { onMounted, ref } from 'vue'
import { getJson, putJson } from '../api'

const rows = ref([])
const choices = ref([])
const err = ref('')
const msg = ref('')
const busy = ref(null)
const operator = ref('')
const draft = ref({})

// 版本台账与策略证明：只读，与策略表 / 隔离行同源。
const versions = ref([])
const proofs = ref([])
const proofFilter = ref({ reason_code: '', policy_version: '' })

async function load() {
  const r = await getJson('/api/quarantine/policies')
  rows.value = r.策略
  choices.value = r.可选策略
  for (const row of rows.value) {
    draft.value[row.reason_code] = row.policy
  }
  versions.value = (await getJson('/api/quarantine/policies/versions')).版本
  await loadProofs()
}

async function loadProofs() {
  const p = new URLSearchParams()
  if (proofFilter.value.reason_code) p.set('reason_code', proofFilter.value.reason_code)
  if (proofFilter.value.policy_version) p.set('policy_version', proofFilter.value.policy_version)
  p.set('limit', '200')
  proofs.value = (await getJson(`/api/quarantine/proofs?${p}`)).证明
}

onMounted(async () => {
  try {
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  }
})

async function save(row) {
  busy.value = row.reason_code
  err.value = ''
  msg.value = ''
  try {
    const r = await putJson(`/api/quarantine/policies/${row.reason_code}`, {
      policy: draft.value[row.reason_code],
      updated_by: operator.value || '未署名',
    })
    msg.value = r.appended
      ? `${r.reason_code} 已追加策略版本 v${r.version}：「${r.policy}」。` +
        `仅影响变更后新决策，历史隔离处置与旧版本快照不改写。`
      : `${r.reason_code} 策略仍为「${r.policy}」，未产生新版本。`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = null
  }
}

function yuan(cents) {
  return (Number(cents || 0) / 100).toFixed(2)
}

function policyClass(p) {
  if (p === '自动可再配') return 'on'
  if (p === '永久拒绝') return 'bad'
  return 'warn'
}

function actionClass(a) {
  if (a === '再配') return 'on'
  if (a === '永久拒绝拦下') return 'bad'
  if (a === '待确认拦下') return 'warn'
  return ''
}
</script>

<template>
  <div>
    <h1>隔离策略</h1>
    <p class="lead">
      策略表按「原因码 + 版本」只追加落库。改策略 = 追加新版本，只影响变更后的新决策；
      历史隔离处置结果与旧版本快照一律不改写。策略证明逐条记录每个隔离决策。
    </p>
    <p v-if="err" class="msg">{{ err }}</p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <div class="row-actions">
      <label class="inline-field">
        操作人
        <input v-model="operator" placeholder="署名" />
      </label>
    </div>

    <div class="panel">
      <h2>原因码策略表 · 当前版本</h2>
      <table>
        <thead>
          <tr>
            <th>原因码</th><th>名称</th><th>含义</th><th>当前策略</th><th>版本</th>
            <th>未平</th><th>未平金额（元）</th><th>维护</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.reason_code">
            <td class="mono">{{ r.reason_code }}</td>
            <td>{{ r.label }}</td>
            <td class="desc">{{ r.description }}</td>
            <td><span class="tag" :class="policyClass(r.policy)">{{ r.policy }}</span></td>
            <td class="mono">v{{ r.version }}</td>
            <td>{{ r.open_count }}</td>
            <td>{{ yuan(r.open_amount_cents) }}</td>
            <td>
              <div class="policy-edit">
                <select v-model="draft[r.reason_code]">
                  <option v-for="c in choices" :key="c" :value="c">{{ c }}</option>
                </select>
                <button
                  class="btn secondary btn-sm"
                  :disabled="busy === r.reason_code || draft[r.reason_code] === r.policy"
                  @click="save(r)"
                >
                  追加版本
                </button>
              </div>
              <p class="hint-text">最近：{{ r.updated_by }} / {{ r.updated_at }}</p>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="8">尚无策略行</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>策略版本台账（只读 · 只追加）</h2>
      <table>
        <thead>
          <tr>
            <th>原因码</th><th>版本</th><th>策略</th><th>是否当前</th><th>维护人</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="v in versions" :key="`${v.reason_code}-${v.version}`">
            <td class="mono">{{ v.reason_code }}</td>
            <td class="mono">v{{ v.version }}</td>
            <td><span class="tag" :class="policyClass(v.policy)">{{ v.policy }}</span></td>
            <td><span class="tag" :class="{ on: v.is_current }">{{ v.is_current ? '当前' : '历史' }}</span></td>
            <td>{{ v.updated_by }}</td>
            <td class="mono">{{ v.updated_at }}</td>
          </tr>
          <tr v-if="!versions.length"><td colspan="6">尚无版本</td></tr>
        </tbody>
      </table>
    </div>

    <div class="panel">
      <h2>策略证明（只读 · 只追加）</h2>
      <div class="row-actions">
        <label class="inline-field">
          原因码
          <select v-model="proofFilter.reason_code" @change="loadProofs">
            <option value="">全部</option>
            <option v-for="r in rows" :key="r.reason_code" :value="r.reason_code">
              {{ r.reason_code }}
            </option>
          </select>
        </label>
        <label class="inline-field">
          策略版本
          <input v-model="proofFilter.policy_version" placeholder="如 2" @change="loadProofs" />
        </label>
      </div>
      <table>
        <thead>
          <tr>
            <th>证明键</th><th>隔离#</th><th>隔离键</th><th>原因码</th>
            <th>策略版本</th><th>策略</th><th>动作</th><th>归并#</th><th>时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="p in proofs" :key="p.proof_key">
            <td class="mono">{{ p.proof_key }}</td>
            <td>{{ p.quarantine_id }}</td>
            <td class="mono">{{ p.quarantine_key }}</td>
            <td class="mono">{{ p.reason_code }}</td>
            <td class="mono">v{{ p.policy_version }}</td>
            <td><span class="tag" :class="policyClass(p.policy)">{{ p.policy }}</span></td>
            <td><span class="tag" :class="actionClass(p.action)">{{ p.action }}</span></td>
            <td>{{ p.settle_run_id ?? '—' }}</td>
            <td class="mono">{{ p.created_at }}</td>
          </tr>
          <tr v-if="!proofs.length"><td colspan="9">暂无策略证明（先跑归并）</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
