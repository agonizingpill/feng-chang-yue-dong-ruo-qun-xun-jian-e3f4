export const ROLES={admin:'蜂场主管',beekeeper:'巡箱蜂师',viewer:'观察员'};
export const CARE_KINDS={feed:'饲喂',requeen:'换王'};
export const CARE_STATES={cancelled:'已取消退料',planned:'待执行',executed:'已执行待复查',rechecked:'复查待关闭',needs_requeen:'无王须换王',closed:'越冬复查已关闭'};
export const VISIT_STATES={recorded:'待筛查',screening:'筛查中',screened:'已筛查',caring:'处置中',followup:'已复查',needs_requeen:'须换王',closed:'已闭环',stale:'历史旧巡检',blocked:'待重试',deferred:'待闭环接纳',conflict:'读数冲突隔离'};
export const HIVE_STATES={observe:'常规观察',needs_care:'需要关注',caring:'处置中',needs_requeen:'待换王',winter_ready:'越冬复查通过',spring_rescue:'春检救护',spring_ready:'春检通过'};
// 补传读数接纳状态：只有 current/accepted 参与当前箱况与筛查。
export const INTAKE_STATES={current:'当前读数',accepted:'当前读数',late:'晚到已保留',deferred:'待闭环接纳',conflict:'冲突隔离中',conflict_lost:'冲突未采用',superseded:'已被更新采集时刻取代',pending:'处理中'};
export const INTAKE_TONE={current:'screened',accepted:'screened',late:'flagged',deferred:'executed',conflict:'needs_requeen',conflict_lost:'stale',superseded:'stale',pending:''};
export const OUTCOMES={accepted:'已成当前箱况',late:'晚到已保留',deferred:'待闭环接纳',conflict:'冲突已隔离',duplicate:'重试未重入库',rejected:'被拒绝'};
