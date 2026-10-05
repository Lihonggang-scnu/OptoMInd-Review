# Ch6 章内编排：微生物组与免疫治疗毒性的关联

状态：已完成（合同通过，全部已选来源有落点）；单元 4/4；段落任务 12；表格任务 1；已安排来源 28；声明未用 15
来源说明：旧模型响应由当前本地程序离线重导（未调用模型，也不代表新提示词生成过该结果）（模型调用：False；旧响应：F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\arrangement\Ch6\raw_responses\chapter-arrangement_20260926-deepseek_Ch6-key0-attempt0.raw）

本章判断：肠道微生物组通过分类学特征、代谢产物及免疫通路调节实体瘤患者对 ICI 的临床疗效，但证据强度因癌种特异性、毒性表型（结肠炎 vs 心肌炎）及干预情境（供体特征、ICI 骨架方案）而异。观察性关联在结肠炎中证据最强，心肌炎等罕见 irAEs 仍受限于样本量；微生物干预（如 FMT）的获益与风险高度依赖于供体菌群组成与联合用药方案的交互作用。目前缺乏 Phase III RCT 证据支持微生物干预的常规临床应用。

## Ch6_U1 特定菌属与 irAEs 的癌种特异性关联及预测模型困境
> 调整说明：将 P0600 从 case_level_uses 整合进 P01，强化因果证据；将 P0049/P0605 整合进 P03，明确样本量局限。

### Ch6_U1_P01 段落任务
- 判断：结肠炎型 irAEs 的微生物指纹与整体多样性下降
- 展开：比较结肠炎型与非结肠炎型 irAE 患者的粪便宏基因组/16S 特征，指出主要产丁酸菌丰度降低、拟杆菌门/双歧杆菌减少及肠球菌富集是结肠炎的核心表型。P0017 通过抗生素预处理小鼠 FMT 实验验证其因果致病潜力：接受结肠炎型患者菌群移植的小鼠出现致命性严重结肠炎（3/9 死亡），而非结肠炎型菌群移植小鼠全部存活。
- 来源 P0017（DOP47 Gut microbiome contributes to the development of immun）｜角色：主论据｜用途：提供结肠炎型患者菌群移植小鼠出现致命性严重结肠炎的因果证据
- 来源 P0600（Gut microbiota composition in patients with advanced maligna）｜角色：比较｜用途：提供肺癌及多癌种队列中结肠炎与非结肠炎 irAEs 的微生物指纹差异，并通过人源化小鼠 FMT 实验确立菌群致结肠炎的因果性

### Ch6_U1_P02 段落任务
- 判断：胃肠道癌症的保护性菌群与器官特异性毒性差异
- 展开：聚焦食管癌、胃癌、结直肠癌队列（P0011，n=95），指出 Ruminococcus callidus 和 Bacteroides xylanisolvens 在无重症 irAEs 患者中显著富集，且尿素循环/精氨酸生物合成通路与低毒性相关。同时强调皮肤、血液、内分泌及肝脏毒性各自呈现独立的微生物通路特征，反驳单一通用标志物假设。但需注意该研究样本量有限且缺乏外部验证。
- 来源 P0011（Correlation of the gut microbiome and immune-related adverse）｜角色：主论据｜用途：提供胃肠道癌症队列中 Ruminococcus callidus 和 Bacteroides xylanisolvens 富集与低毒性相关的实证数据

### Ch6_U1_P03 段落任务
- 判断：预测模型的构建困境与非结肠炎 irAEs 证据不足
- 展开：评估基于机器学习（如 RF14 分类器，P0308）的 irAEs 预测模型性能，指出尽管 AUC 可达 0.88，但在多重检验校正后部分特征显著性不足。P0312 综述指出，即使在大样本队列中，非结肠炎 irAEs（如心肌炎、神经毒性）的样本量仍不足以支持独立的微生物组分析，当前证据主要集中于结肠炎。不同研究间测序平台、CTCAE 分级版本及队列异质性导致模型泛化能力受限。
- 来源 P0308（Gut microbiome for predicting immune checkpoint blockade-ass）｜角色：主论据｜用途：提供 RF14 分类器性能数据及多重检验校正后特征显著性不足的证据
- 来源 P0312（Biomarkers for immune-related adverse events in cancer patie）｜角色：背景｜用途：综述指出非结肠炎 irAEs 样本量不足以支持独立微生物组分析的限制
- 来源 P0049（Gut microbiome and immune checkpoint inhibitor toxicity.）｜角色：局限｜用途：揭示 F. prausnitzii 等标志物在多重检验下的不一致性及大样本队列中非结肠炎亚组样本量极小（n=2）的现状
- 来源 P0605｜角色：局限｜用途：量化心肌炎/脑膜炎等亚组样本量极小，直接支撑预测模型构建困境及非结肠炎毒性证据不足的论述

## Ch6_U2 疗效与毒性的耦合及解耦关系与免疫稳态中介

### Ch6_U2_P01 段落任务
- 判断：共享免疫激活通路的"双刃剑"效应
- 展开：指出高丰度 Akkermansia muciniphila 在 NSCLC 队列中与延长 OS 正相关，但同时增加 irAEs 风险（P0260），提示微生物介导的 CD8+ T 细胞活化与 IFN-γ分泌是疗效提升与自身免疫毒性共有的上游驱动力。这种关联是观察性的，不能推断因果。
- 来源 P0260（The Evolving Landscape of Immunotoxicity: Charting Mechanism）｜角色：主论据｜用途：提供 Akkermansia muciniphila 与 OS 正相关但增加 irAEs 风险的观察性关联证据

### Ch6_U2_P02 段落任务
- 判断：解耦路径：特定菌属与无毒性响应的共存
- 展开：对比黑色素瘤纵向队列数据（P0239），发现 Eubacterium siraeum 高丰度与无 irAEs 的客观缓解状态显著相关，且短链脂肪酸产生菌（如 Coprococcus comes, Blautia glucerasea）在未发生 irAEs 的患者中同样富集（P0428），挑战"毒性必然预示疗效"的直觉假设。但需注意这些发现主要来自回顾性分析。
- 来源 P0239（De-correlating immune checkpoint inhibitor toxicity and resp）｜角色：主论据｜用途：提供 Eubacterium siraeum 高丰度与无 irAEs 的客观缓解状态显著相关的纵向队列数据
- 来源 P0428（Comparison of baseline gut microbiome features between respo）｜角色：例证｜用途：证实 SCFA 产生菌在无 irAEs 响应者中显著富集，为解耦疗效与毒性提供直接的表型证据
- 来源 P0314（The Association of the Microbiome with Melanoma Tumor Respon）｜角色：例证｜用途：识别 Blautia luti 等 Lachnospiraceae 成员与“有效且无 irAE"状态的独立关联，补充代谢功能菌群线索

### Ch6_U2_P03 段落任务
- 判断：基线免疫稳态与纵向动态变化的中介作用
- 展开：整合黑色素瘤配对粪便与外周血 PBMC 纵向数据（P0098，n=126），显示基线拟杆菌属（Bacteroides）高丰度与治疗后 Tregs 频率显著下降相关，且 Tregs 耗竭程度直接预测严重 irAEs 的发生；相反，瘤胃球菌科（Ruminococcaceae）主导的基线状态则与更好的治疗响应及较低的毒性风险同步出现。
- 来源 P0098（Abstract 3463: Longitudinal microbiome-immune dynamics in me）｜角色：主论据｜用途：提供基线拟杆菌属高丰度与 Tregs 频率下降及严重 irAEs 相关的纵向免疫细胞动态数据
- 来源 P0049（Gut microbiome and immune checkpoint inhibitor toxicity.）｜角色：发展｜用途：揭示基线病原共生菌富集与治疗早期产丁酸菌耗竭的时序特征，为免疫稳态失衡提供临床动态证据
- 来源 P0605｜角色：发展｜用途：通过纵向宏基因组数据证实 Ruminococcaceae 动态耗竭与严重 irAEs 关联，补充免疫稳态失衡的微生物组证据

## Ch6_U3 微生物干预的安全性与供体特征的情境依赖性

### Ch6_U3_P01 段落任务
- 判断：FMT-LUMINate 试验的疗效分化与毒性起病时间提前
- 展开：报告 NSCLC 队列（抗 PD-1 单药，n=20）ORR 达 80% 且无≥3 级 AEs；而黑色素瘤队列（抗 PD-1+ 抗 CTLA-4 双免，n=20）ORR 为 75%，但 65% 患者经历≥3 级 AEs，其中腹泻/结肠炎占 20%，心肌炎发生率高达 15%（文献报道通常<1%），且严重毒性中位起病时间提前至 40 天（P0583）。需明确这是 Phase II 单臂试验，非 Phase III RCT。
- 来源 P0583（Fecal microbiota transplantation plus immunotherapy in non-s）｜角色：主论据｜用途：报告 FMT-LUMINate 试验中双免背景下 ORR 75% 但 65% 患者经历≥3 级 AEs 及心肌炎发生率高达 15% 的数据
- 来源 P0578（Safety and efficacy of fecal microbiota transplantation in s）｜角色：比较｜用途：提供 MITRIC 试验在难治人群中 ORR 0% 的结果，对比显示干预效果在初治与难治人群中的异质性
- 来源 P0212（Microbiota boost immunotherapy? A meta-analysis dives into f）｜角色：背景｜用途：荟萃分析量化 FMT 联合 ICI 的总体 ORR 及 3-4 级 AE 发生率，为安全性边界提供定量基准

### Ch6_U3_P02 段落任务
- 判断：供体微生物组聚类与特定毒性事件的因果关联
- 展开：基于 Bray-Curtis 指数无监督聚类将健康供体分为 Cluster A 与 Cluster B（含供体 5、11）。Cluster B 以 Segatella copri、Prevotella sp. Marseille-P4119 等高丰度为特征。接受 Cluster B 供体 FMT 的黑色素瘤患者中，这些菌属显著富集并与≥3 级 AEs 强相关；进一步流式分析显示 S. copri 定植与外周血 CD4+ T 细胞亚群比例增加相关，且该毒性仅在双 ICI 背景下显现，单药抗 PD-1 治疗中未观察到类似风险（P0583）。但 Prevotella 诱导心肌炎的具体分子机制（抗原或代谢物）尚未解析。
- 来源 P0583（Fecal microbiota transplantation plus immunotherapy in non-s）｜角色：主论据｜用途：提供供体无监督聚类结果及 Cluster B（Prevotella 富集）与≥3 级 AEs 强相关的因果关联证据

### Ch6_U3_P03 段落任务
- 判断：益生菌的双向调节与伴随用药的干扰效应
- 展开：指出特定共生菌（如 LGG）可通过释放脂磷壁酸激活巨噬细胞保护肠上皮，但在免疫抑制状态下存在机会性感染风险（P0040）；同时汇总药物警戒数据，显示 PPI 联用可能增加肝胆及皮肤毒性报告率（P0088），而近期广谱抗生素暴露不仅削弱疗效，还独立增加严重结肠炎风险（P0310）。
- 来源 P0040（Benefits of using probiotics as adjuvants in anticancer ther）｜角色：主论据｜用途：指出 LGG 激活巨噬细胞保护肠上皮但在免疫抑制状态下存在机会性感染风险
- 来源 P0088（Adverse reactions of immune checkpoint inhibitors combined w）｜角色：例证｜用途：汇总药物警戒数据，显示 PPI 联用可能增加肝胆及皮肤毒性报告率
- 来源 P0310（Association of antibiotic exposure with overall survival and）｜角色：例证｜用途：显示近期广谱抗生素暴露独立增加严重结肠炎风险
- 来源 P0357（Leveraging beneficial microbiome-immune interactions via pro）｜角色：发展｜用途：系统梳理益生菌菌株特异性效应及商业制剂异质性，阐明其在调节 irAEs 中的双向作用及定植挑战

### Ch6_U3_T01 表格任务
- 目的：比较 FMT 供体聚类特征与 ICI 方案交互下的毒性风险
- 列：供体聚类 | 特征菌属 | ICI 方案背景 | 主要毒性事件 | 发生率/关联强度
  - 第 1 行：Cluster A (非 Prevotella 富集)｜来源：P0583(比较)
  - 第 2 行：Cluster B (Prevotella/Segatella 富集)｜来源：P0583(比较)

## Ch6_U4 介导毒性的分子与免疫通路机制

### Ch6_U4_P01 段落任务
- 判断：肠道屏障受损与内毒素易位的促炎级联
- 展开：描述 Akkermansia 等黏液降解菌减少或致病菌扩张导致的紧密连接蛋白（ZO-1, Occludin）表达下调，促使 LPS 入血并持续激活 TLR4/NF-κB 通路，为全身性 irAEs 提供持续的先天免疫刺激背景（P0217）。
- 来源 P0217（Myokine-mediated mechanisms of immune checkpoint inhibitors-）｜角色：主论据｜用途：描述 Akkermansia 等黏液降解菌减少致 LPS 入血并激活 TLR4/NF-κB 通路的机制
- 来源 P0284（Metagenomics and culturomics reveal the dual role of the gut）｜角色：例证｜用途：揭示 Paraclostridium bifermentans 通过活菌破坏上皮屏障致结肠炎的具体机制

### Ch6_U4_P02 段落任务
- 判断：代谢物介导的表观遗传重编程与炎症抑制/促进
- 展开：指出非 irAEs 患者肠道中甲萘醌（维生素 K2）生物合成途径（menH/menC 基因）显著富集，血清甲萘醌水平更高，其可能通过抑制 NF-κB 通路发挥抗炎保护作用（P0308）；反之，SCFAs 在特定浓度下可通过 HDAC 抑制影响 Treg/Th17 分化平衡，剂量依赖性决定了其是维持稳态还是加剧炎症（P0472）。
- 来源 P0308（Gut microbiome for predicting immune checkpoint blockade-ass）｜角色：主论据｜用途：指出甲萘醌生物合成途径富集与血清甲萘醌水平更高及抗炎保护作用的关联
- 来源 P0472（Immunometabolism: The role of gut‐derived microbial metaboli）｜角色：主论据｜用途：阐述 SCFAs 通过 HDAC 抑制影响 Treg/Th17 分化平衡的剂量依赖性效应
- 来源 P0462（Gut microbiome metabolites, molecular mimicry, and species-l）｜角色：发展｜用途：补充肠道微生物组代谢物与分子模拟驱动长期疗效和不良事件结局的机制线索

### Ch6_U4_P03 段落任务
- 判断：MyD88 依赖的髓系激活与心肌炎中的 CD8+ T 细胞 TNF 轴
- 展开：综合临床前与转录组证据，阐明微生物抗原通过 MyD88 通路激活巨噬细胞分泌 TNFα/IL-6 导致肝损伤（P0126），或在 ICI 心肌炎中驱动 CD8+ T 细胞释放 TNF 并通过 TNFR2 信号招募髓系细胞、诱发心律失常（P0149）。结合 HLA 限制性抗原呈递框架，指出细菌肽段（如β-半乳糖苷酶）与心脏肌球蛋白的分子模拟可交叉激活自体反应性 CD4+/CD8+ T 细胞（P0597），但 Prevotella 特异性机制尚未解析。
- 来源 P0126（More fuel for the fire: Gut microbes and toxicity to immune ）｜角色：主论据｜用途：阐述肠道微生物通过 MyD88 通路激活巨噬细胞/CD8+ T 细胞引发肝损伤的炎症级联
- 来源 P0149（Immune checkpoint inhibitor-induced myocarditis is dependent）｜角色：主论据｜用途：提供 ICI 心肌炎中 CD8+ T 细胞释放 TNF 并通过 TNFR2 信号诱发心律失常的机制证据
- 来源 P0597（Human Leukocyte Antigen Determinants in Myocarditis and Dila）｜角色：主论据｜用途：结合 HLA 限制性抗原呈递框架，指出细菌肽段与心脏肌球蛋白的分子模拟机制
- 来源 P0602（Abnormal gut microbiota may cause PD-1 inhibitor-related car）｜角色：例证｜用途：提供 PD-1 抑制剂诱导心肌炎的动物实验证据，阐明肠道菌群失调通过抑制心脏局部 Treg 功能导致炎症的机制
- 来源 P0604（Immunomodulatory Role of Tenascin-C in Myocarditis and Infla）｜角色：发展｜用途：综述 TN-C 介导的树突状细胞激活与 Th17 极化通路，以及特定肠道菌群通过分子模拟触发心脏自身免疫反应的实例
- 来源 P0598（The gut microbiota: an emerging therapeutic target for ICI-a）｜角色：发展｜用途：提供肠道微生物组作为 ICI 相关心肌炎新兴治疗靶点的理论框架与机制综述

## 声明不使用的来源
- P0067：聚焦三阴性乳腺癌化疗 - 免疫联合治疗的预后，非 ICI 单药毒性机制，超出本章 ICI 毒性核心范围
- P0146：一般性综述，无具体 ICI 毒性数据或机制细节，被本章具体原始研究覆盖
- P0197：FMT-LUMINate 试验摘要，已被 P0583 全文数据 superseded
- P0261：一般性干预综述，缺乏具体毒性数据，被 P0583/P0212 等具体试验数据覆盖
- P0297：肺癌微生物组综述，未提供具体 ICI 毒性机制数据，被本章具体机制论文覆盖
- P0319：一般性机制综述，无具体 ICI 毒性数据，被本章具体机制论文覆盖
- P0409：一般性 TME 互作综述，无具体 ICI 毒性数据，被本章具体机制论文覆盖
- P0447：代谢物一般性综述，无具体 ICI 毒性数据，被 P0472/P0308 覆盖
- P0460：精准健康一般性综述，超出本章 ICI 毒性机制范围
- P0486：结直肠癌代谢物靶向治疗综述，超出本章 ICI 毒性机制范围
- P0590：结直肠癌微生物组综述，无具体 ICI 毒性数据，被 P0011 覆盖
- P0595：心肌炎免疫基因研究，未明确涉及微生物组关联，超出本章微生物组主题
- P0596：心肌炎免疫基因研究，未明确涉及微生物组关联，超出本章微生物组主题
- P0601：婴儿双歧杆菌基因组测序，无 ICI 毒性背景，超出本章范围
- P0609：空间代谢组学摘要，无具体 ICI 毒性数据，被本章具体机制论文覆盖

## 来源目录（本地补全，写作者据此取素材）
- P0011｜Correlation of the gut microbiome and immune-related adverse events in gastroint…｜2023｜DOI 10.3389/fcimb.2023.1099063｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\042544f408b07e2d51c57f40f4df614d0c0960a7\744aa1560618c1f5e01bef359713a1d9\attempt-01\PAPER_READING_CARD.json#P0011｜用于 Ch6_U1_P02
- P0017｜DOP47 Gut microbiome contributes to the development of immune checkpoint inhibit…｜2023｜DOI 10.1093/ecco-jcc/jjac190.0087｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\08d60ce7ff453c5ae2b4c3a47581b79e02b2b5a0\3cdc3bd861fdb8a127db2669e6af85fa\attempt-01\PAPER_READING_CARD.json#P0017｜用于 Ch6_U1_P01
- P0040｜Benefits of using probiotics as adjuvants in anticancer therapy (Review)｜2019｜DOI 10.3892/wasj.2019.13｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\1102f3c335bd628a6e744f20e222d1993cf1250b\06a574462ddd7a904161a25eb58d7e53\attempt-02\PAPER_READING_CARD.json#P0040｜用于 Ch6_U3_P03
- P0049｜Gut microbiome and immune checkpoint inhibitor toxicity.｜2025｜DOI 10.1016/j.ejca.2025.115221｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\1607ffdb79bd5dbb1f94932e27410c55f0aa8290\e9a69606b703c06cd1c1d609fc8e8730\attempt-01\PAPER_READING_CARD.json#P0049｜用于 Ch6_U1_P03, Ch6_U2_P03｜别名 P0605
- P0067｜Gut microbiota diversity is prognostic and associated with benefit from chemo‐im…｜2024｜DOI 10.1002/1878-0261.13760｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\19590ba8c3221ee742d0e85768b8d22521dcbb86\6d181a11d5423db7f05059b5076ba436\attempt-01\PAPER_READING_CARD.json#P0067
- P0088｜Adverse reactions of immune checkpoint inhibitors combined with Proton pump inhi…｜2024｜DOI 10.1186/s12885-024-12947-7｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\2129f97a58fe871598a9da0cb18083d6d6fa4729\4e1b93af88df541623acee2d53ed5b51\attempt-01\PAPER_READING_CARD.json#P0088｜用于 Ch6_U3_P03
- P0098｜Abstract 3463: Longitudinal microbiome-immune dynamics in melanoma patients trea…｜2023｜DOI 10.1158/1538-7445.am2023-3463｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\2709ffca937655fdff12c871b14cb93ee1830d79\b33f79e777278a549aec748fde5129d1\attempt-01\PAPER_READING_CARD.json#P0098｜用于 Ch6_U2_P03
- P0126｜More fuel for the fire: Gut microbes and toxicity to immune agonist antibodies i…｜2021｜DOI 10.1016/j.xcrm.2021.100482｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\378cf66d179d103b23a392cdb8e99a650e346e11\d28e76ec01ced01ab012380e4cb24a84\attempt-01\PAPER_READING_CARD.json#P0126｜用于 Ch6_U4_P03
- P0146｜Gut microbiota shapes cancer immunotherapy responses｜2025｜DOI 10.1038/s41522-025-00786-8｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\4171288ecca93c6bca3eee61295418d704bb00fd\92125422c10f28cdb7938d2658870c58\attempt-01\PAPER_READING_CARD.json#P0146
- P0149｜Immune checkpoint inhibitor-induced myocarditis is dependent on CD8 T cell-deriv…｜2026｜DOI 10.1084/jem.20251717｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\433eca5f16060d9f68630dfe36b2383cf38b373a\2a05f0764a1b0c823e740054b7cf1dde\attempt-01\PAPER_READING_CARD.json#P0149｜用于 Ch6_U4_P03
- P0197｜Abstract 2210: Microbiome profiling reveals that fecal microbiota transplantatio…｜2025｜DOI 10.1158/1538-7445.am2025-2210｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\55e5f184350846573ce6b2d40fdcbd15f2602f90\fe6455704805c8dbacd86cc388cca0ba\attempt-01\PAPER_READING_CARD.json#P0197
- P0212｜Microbiota boost immunotherapy? A meta-analysis dives into fecal microbiota tran…｜2025｜DOI 10.1186/s12916-025-04183-y｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\5af3aee4ca5f3778325d44c8432c8b71b95ca531\8c1809a73adaf485fed884559e8e0215\attempt-01\PAPER_READING_CARD.json#P0212｜用于 Ch6_U3_P01
- P0217｜Myokine-mediated mechanisms of immune checkpoint inhibitors-associated colorecta…｜2026｜DOI 10.3389/fimmu.2026.1852152｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\5babbfef7caecfce48f24a69bac030cd4c5dd4e0\a08eabde843b0f5b6be7307e7e78bad8\attempt-01\PAPER_READING_CARD.json#P0217｜用于 Ch6_U4_P01
- P0239｜De-correlating immune checkpoint inhibitor toxicity and response in melanoma via…｜2023｜DOI 10.1200/jco.2023.41.16_suppl.9569｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\645b681b7557677d115a714d338b0565b4118b06\5feaddfbf68a0e20605a6b160436391b\attempt-01\PAPER_READING_CARD.json#P0239｜用于 Ch6_U2_P02
- P0260｜The Evolving Landscape of Immunotoxicity: Charting Mechanisms and Future Strateg…｜2025｜DOI 10.1002/mdr2.70019｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\6f8044dc50ea73203f972ec2f8a2e2527694e3d3\024abf8a881fc6ce5a053a7eebb5478f\attempt-01\PAPER_READING_CARD.json#P0260｜用于 Ch6_U2_P01
- P0261｜Facts and Hopes for Gut Microbiota Interventions in Cancer Immunotherapy｜2022｜DOI 10.1158/1078-0432.ccr-21-1129｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\6f9060daa22a7c9efcd4a92919035c5e5c24367a\cc59b1602065d8843b6a0ee6c2797d9c\attempt-01\PAPER_READING_CARD.json#P0261
- P0284｜Metagenomics and culturomics reveal the dual role of the gut microbiome in the d…｜2026｜DOI 10.1186/s40168-026-02419-4｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\7cdc8a62c7a781f0b9a88abe52934ab9b889134d\25b15fdd88b6298d598d73dd0806a771\attempt-01\PAPER_READING_CARD.json#P0284｜用于 Ch6_U4_P01
- P0297｜The gut microbiome in lung cancer: from pathogenesis to precision therapy｜2025｜DOI 10.3389/fmicb.2025.1606684｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\8211fc48bb00f738157c8859ad0748cc7d220f09\5faefb771e9af4b8f1b5bd10a8bf9492\attempt-01\PAPER_READING_CARD.json#P0297
- P0308｜Gut microbiome for predicting immune checkpoint blockade-associated adverse even…｜2024｜DOI 10.1186/s13073-024-01285-9｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\8524fbf4592e1ca625332b35344ad1a5d37aad93\a7d73d52ee2de75a512990925c2135d9\attempt-01\PAPER_READING_CARD.json#P0308｜用于 Ch6_U1_P03, Ch6_U4_P02
- P0310｜Association of antibiotic exposure with overall survival and colitis in patients…｜2020｜DOI 10.1200/jco.2020.38.5_suppl.56｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\85a36859d78332d758a8b240a964fcdac76937f6\fba9e4d7ee5c0580e3808b29665ae162\attempt-01\PAPER_READING_CARD.json#P0310｜用于 Ch6_U3_P03
- P0312｜Biomarkers for immune-related adverse events in cancer patients treated with imm…｜2024｜DOI 10.1093/jjco/hyad184｜材料深度 structured_partial｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\85e23d9e543859acdb7e57cd52f3d28495c0162e\65ef0e5520c1328b790d581bc72634b3\attempt-01\PAPER_READING_CARD.json#P0312｜用于 Ch6_U1_P03
- P0314｜The Association of the Microbiome with Melanoma Tumor Response to Immune Checkpo…｜2025｜DOI 10.1101/2025.01.30.25321413｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\873fa8f3f156b691e03d1431a71f79cf1fa30374\435a1b59a10bc4c190a34f4d3943c831\attempt-01\PAPER_READING_CARD.json#P0314｜用于 Ch6_U2_P02
- P0319｜Interplay between Gut Microbiome, Metabolites, and Tumor Immunity: Mechanisms an…｜2026｜DOI 10.53941/hm.2026.100010｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\896aa96e6dbaae9b8dbaa7d183f459500ad70834\0a3462ca68e00befd1fc57bcd3fddfc4\attempt-01\PAPER_READING_CARD.json#P0319
- P0357｜Leveraging beneficial microbiome-immune interactions via probiotic use in cancer…｜2025｜DOI 10.3389/fimmu.2025.1713382｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\9bda8266e0dc2c5949effec25ef1dea48f7de6ee\b6c6cd21701a7a981a626866ad6f38a8\attempt-01\PAPER_READING_CARD.json#P0357｜用于 Ch6_U3_P03
- P0409｜Gut Microbiota–Tumor Microenvironment Interactions: Mechanisms and Clinical Impl…｜2025｜DOI 10.2147/cmar.s405590｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\b32c1543240c066ebf3d3e797ec04127cb831f40\f06964339446851902a2e50d606b8528\attempt-01\PAPER_READING_CARD.json#P0409
- P0428｜Comparison of baseline gut microbiome features between response subgroups of pat…｜2026｜DOI 10.1200/jco.2026.44.16_suppl.9528｜材料深度 abstract｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\bb6b1be372fbcd8282307d93b91745ca4c844e92\570bf3edfaf1d9cb05bce8eee899d7ab\attempt-01\PAPER_READING_CARD.json#P0428｜用于 Ch6_U2_P02
- P0447｜Microbial metabolites and their influence on the tumor microenvironment｜2025｜DOI 10.3389/fimmu.2025.1675677｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\c59738fb3db453360d9f86e97ad338176abe8eb6\42993142a6b2e557adda307d103ec858\attempt-01\PAPER_READING_CARD.json#P0447
- P0460｜Therapeutic targeting of the host-microbiota-immune axis: implications for preci…｜2025｜DOI 10.3389/fimmu.2025.1570233｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\cbbd16613d73a6baa4dff169701c792f0d38e1e2\148d0bec6459003d47e990fa0dd471c5\attempt-01\PAPER_READING_CARD.json#P0460
- P0462｜Gut microbiome metabolites, molecular mimicry, and species-level variation drive…｜2024｜DOI 10.1016/j.ebiom.2024.105427｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\cc2b555e2b10a62d0141cb12ab5cefa6e47d570c\e6d0d6ef9a5d062d4cdc8e2e4fb5ee85\attempt-01\PAPER_READING_CARD.json#P0462｜用于 Ch6_U4_P02
- P0472｜Immunometabolism: The role of gut‐derived microbial metabolites in optimising im…｜2025｜DOI 10.1002/ctm2.70472｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\d16dfaee360f4d850878948581e3cdae9911db48\f886c7df747f315affa40a5c99b5df03\attempt-01\PAPER_READING_CARD.json#P0472｜用于 Ch6_U4_P02
- P0486｜Targeting Microbiome Metabolites: Reshaping Immunotherapy and Clinical Managemen…｜2026｜DOI 10.1002/imm3.70030｜位置 F:\OptoMind-Review-2\outputs\paper_cards\20260922_microbiome_batch\reading\cards\daf9a1dede2bf1241ba0b187914be958d46b79fd\0b465d9d1ce55910ec376a98185f4f08\attempt-01\PAPER_READING_CARD.json#P0486
- P0578｜Safety and efficacy of fecal microbiota transplantation in solid cancers resista…｜2026｜DOI 10.1136/jitc-2026-015122｜位置 F:\OptoMind-Review-2\outputs\progressive_review_plan\20260924_microbiome\run01\level1\supplements\large_scale_rct_results\source_units\SU02_search_candidate_fc8af41423\card\PAPER_READING_CARD.json#P0578｜用于 Ch6_U3_P01
- P0583｜Fecal microbiota transplantation plus immunotherapy in non-small cell lung cance…｜2026｜DOI 10.1038/s41591-025-04186-5｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\progressive_review_plan\20260926_adaptive_retrieval\03_external_closure\runs\case_E_phase3_rct\source_units\SU01_search_candidate_dcbe39b3d2\card\PAPER_READING_CARD.json#P0583｜用于 Ch6_U3_P01, Ch6_U3_P02, Ch6_U3_T01
- P0590｜Gut microbiome in colorectal cancer: recent advances and clinical implications｜2026｜DOI 10.3393/ac.2026.00010.0001｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\level2\external\N48436b3aa73aea86f35f-f45b87be8e30256e\round_1\supplements\phase_iii_rct_evidence\attempt-c7j_i2fn\source_units\SU02_search_candidate_a86301e06d\card\PAPER_READING_CARD.json#P0590
- P0595｜Abstract 4136727: The role of immune mechanism in Immune Checkpoint Inhibitor-As…｜2024｜DOI 10.1161/circ.150.suppl_1.4136727｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N64e929a3f9de2888bd6a-5a6b848383c152a3\round_1\supplements\Ch6_gap_4\attempt-adnnjw8w\source_units\SU01_search_candidate_9277696331\card\PAPER_READING_CARD.json#P0595
- P0596｜Key immune related genes and potential therapeutic targets in immune checkpoint …｜2025｜DOI 10.1093/eurheartj/ehaf784.4157｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N64e929a3f9de2888bd6a-5a6b848383c152a3\round_1\supplements\Ch6_gap_4\attempt-adnnjw8w\source_units\SU02_search_candidate_1ade57df76\card\PAPER_READING_CARD.json#P0596
- P0597｜Human Leukocyte Antigen Determinants in Myocarditis and Dilated Cardiomyopathy｜2026｜DOI 10.1007/s11897-026-00761-0｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N64e929a3f9de2888bd6a-5a6b848383c152a3\round_1\supplements\Ch6_gap_4\attempt-adnnjw8w\source_units\SU03_search_candidate_caa7d3faf8\card\PAPER_READING_CARD.json#P0597｜用于 Ch6_U4_P03
- P0598｜The gut microbiota: an emerging therapeutic target for ICI-associated myocarditi…｜2026｜DOI 10.3389/fcimb.2026.1752485｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N4452dfb25f491e9335fe-5a6b848383c152a3\round_1\supplements\Ch6_gap_5\attempt-fc662a0y\source_units\SU01_search_candidate_634b345179\card\PAPER_READING_CARD.json#P0598｜用于 Ch6_U4_P03
- P0600｜Gut microbiota composition in patients with advanced malignancies experiencing i…｜2023｜DOI 10.3389/fimmu.2023.1109281｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N4452dfb25f491e9335fe-5a6b848383c152a3\round_1\supplements\Ch6_gap_5\attempt-fc662a0y\source_units\SU03_search_candidate_cef6ae84bc\card\PAPER_READING_CARD.json#P0600｜用于 Ch6_U1_P01
- P0601｜Whole genomic sequence analysis of Bacillus infantis: defining the genetic bluep…｜2016｜DOI 10.1186/s12864-016-2900-2｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N64e929a3f9de2888bd6a-5a6b848383c152a3\round_2\supplements\Ch6_gap_4\attempt-x8duln8o\source_units\SU02_search_candidate_d1274441e4\card\PAPER_READING_CARD.json#P0601
- P0602｜Abnormal gut microbiota may cause PD-1 inhibitor-related cardiotoxicity via supp…｜2025｜DOI 10.1038/s41598-025-05635-4｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N4452dfb25f491e9335fe-5a6b848383c152a3\round_2\supplements\Ch6_gap_5\attempt-u2gi65cq\source_units\SU02_search_candidate_65a7baa93c\card\PAPER_READING_CARD.json#P0602｜用于 Ch6_U4_P03
- P0604｜Immunomodulatory Role of Tenascin-C in Myocarditis and Inflammatory Cardiomyopat…｜2021｜DOI 10.3389/fimmu.2021.624703｜材料深度 fulltext｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\chapters\external\N64e929a3f9de2888bd6a-5a6b848383c152a3\round_3\supplements\Ch6_gap_4\attempt-nf1aflvz\source_units\SU01_search_candidate_ec192008c0\card\PAPER_READING_CARD.json#P0604｜用于 Ch6_U4_P03
- P0609｜Abstract B028: Multimodal approach to characterization of metabolism and immune …｜2025｜DOI 10.1158/1557-3265.targetedtherap-b028｜位置 F:\OptoMind-Review-2\outputs\body_full_staged_acceptance_20261004_40cny\planning\writer_packets\Ch6.json#P0609
