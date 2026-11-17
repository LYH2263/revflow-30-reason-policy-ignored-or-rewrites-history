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

async function load() {
  const r = await getJson('/api/quarantine/policies')
  rows.value = r.策略
  choices.value = r.可选策略
  for (const row of rows.value) {
    draft.value[row.reason_code] = row.policy
  }
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
    msg.value = `${r.reason_code} 策略已改为「${r.policy}」（${r.updated_by}）`
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
</script>

<template>
  <div>
    <h1>隔离策略</h1>
    <p class="lead">
      原因码策略表落库（每码一行、互斥策略）。改动同步刷新隔离项与历史决策上的策略标签；
      不是工单审批流，也不做质检签发。
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
      <h2>原因码策略表</h2>
      <table>
        <thead>
          <tr>
            <th>原因码</th><th>名称</th><th>含义</th><th>当前策略</th>
            <th>未平</th><th>未平金额（元）</th><th>维护</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in rows" :key="r.reason_code">
            <td class="mono">{{ r.reason_code }}</td>
            <td>{{ r.label }}</td>
            <td class="desc">{{ r.description }}</td>
            <td><span class="tag" :class="policyClass(r.policy)">{{ r.policy }}</span></td>
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
                  保存
                </button>
              </div>
              <p class="hint-text">最近：{{ r.updated_by }} / {{ r.updated_at }}</p>
            </td>
          </tr>
          <tr v-if="!rows.length"><td colspan="7">尚无策略行</td></tr>
        </tbody>
      </table>
    </div>
  </div>
</template>
