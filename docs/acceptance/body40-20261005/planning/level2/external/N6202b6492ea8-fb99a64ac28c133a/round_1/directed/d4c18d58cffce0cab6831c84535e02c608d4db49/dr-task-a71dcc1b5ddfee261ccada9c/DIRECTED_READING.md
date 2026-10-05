## Q01

跨队列黑色素瘤研究中‘无通用标志物’的统计依据主要源于巨大的队列间异质性。首先，PERMANOVA分析证实“队列”本身是解释微生物群落组成变异的最大因素，其影响远超临床或人口统计学变量。其次，机器学习模型的跨队列泛化能力极差，内部验证表现尚可但外部验证（跨队列测试）的AUC-ROC值普遍较低且波动大，表明基于特定队列训练的微生物特征无法可靠地预测其他队列的反应。最后，尽管荟萃分析能识别出一些在多个队列中趋势一致的物种（如Roseburia spp.、Akkermansia muciniphila），但没有任何单一物种在所有数据集中达到统计学显著性并成为普适性生物标志物。这种不一致性被归因于人群特异性特征、样本处理和分析的方法学选择、饮食和药物使用的差异，以及微生物信号的功能相关性与队列内在特性。作者通过结直肠癌数据的对照分析排除了方法学缺陷的可能性，确认了黑色素瘤ICI反应中微生物关联的复杂性和队列依赖性。

### Concrete examples

- **本研究:** Use: 提供跨队列黑色素瘤免疫检查点抑制剂（ICI）治疗反应研究中‘无通用标志物’的具体统计证据及异质性来源分析，作为直接材料。. 在整合5个新测序队列（n=165）和4个公开队列（n=147）后，PERMANOVA分析显示“队列”因素对微生物群落组成的解释方差是其他任何变量（包括临床参数）的近十倍。机器学习模型验证显示，基于物种丰度的预测能力在不同队列间缺乏可重复性：仅在PRIMM-NL中PFS12预测AUC-ROC为0.64，在PRIMM-UK中ORR预测AUC-ROC为0.78；而在跨队列测试中，仅约31.4%的情况AUC-ROC超过0.6，且留一法交叉验证的平均AUC-ROC仅为0.59-0.60。尽管通过荟萃分析识别出与反应者相关的物种面板（如Bifidobacterium pseudocatenulatum, Roseburia spp., Akkermansia muciniphila），但没有任何单一细菌在所有数据集中成为完全一致的生物标志物。相比之下，结直肠癌数据的相同方法显示出强且一致的生物标志物，证明该结果并非由分析方法导致。 Conditions: 研究对象为接受ICI治疗的晚期皮肤黑色素瘤患者；终点指标为客观缓解率（ORR）和12个月无进展生存期（PFS12）；分析方法包括PERMANOVA、Lasso机器学习模型（100次重复五折分层交叉验证）、以及包含8种差异丰度方法的随机效应荟萃分析；比较对象包括PRIMM-UK、PRIMM-NL、Manchester、Leeds、Barcelona队列及4个公开数据集（Matson et al., Gopalakrishnan et al., Frankel et al., WindTT_2020等）。
- **Routy et al. (2018):** Use: 作为背景研究，展示早期单队列研究中发现的特定菌种关联，用于对比说明当前研究中观察到的不一致性和缺乏共识的现象。. 发现ICI反应者中Akermansia muciniphila、Alistipes属和厚壁菌门（Firmicutes）的相对丰度显著高于非反应者。 Conditions: 针对上皮肿瘤患者的PD-1免疫疗法反应研究；使用宏基因组测序分析粪便样本。 [R11]
- **Gopalakrishnan et al. (2018):** Use: 作为背景研究，展示另一项早期独立研究发现的不同菌种关联，进一步佐证不同研究间结果的差异性。. 发现ICI反应者中Faecalibacterium prausnitzii的相对丰度高于非反应者。 Conditions: 针对黑色素瘤患者抗PD-1免疫疗法反应的研究；使用宏基因组测序分析肠道微生物组。 [R6]
- **Matson et al. (2018):** Use: 作为背景研究，展示第三项独立研究发现的菌种组合关联，强调人类环境中缺乏统一共识的现状。. 发现PD-1治疗的反应性由一组八个物种的增加定义，主要由Bifidobacterium longum驱动。 Conditions: 针对转移性黑色素瘤患者抗PD-1疗法反应的研究；关注共生微生物组。 [R5]
- **Frankel et al. (2017):** Use: 作为背景研究，展示微生物组差异可能取决于ICI治疗方案，并指出某些菌种（如Bacteroides caccae）在不同方案中普遍富集，但同样未形成跨研究的通用标志物。. 报告微生物组因ICI治疗方案而异，但在接受任何ICI方案的反应者中，Bacteroides caccae的富集是常见的。 Conditions: 针对黑色素瘤患者的宏基因组鸟枪法和代谢组学分析；关注不同ICI方案下的微生物组和代谢物。 [R12]

Open points: 具体哪些临床协变量（如PPI使用、既往治疗）在多大程度上介导了队列间的微生物差异及其对ICI反应的混淆作用，虽有提及但未深入量化其交互机制。; 菌株水平的特异性如何具体影响跨队列的一致性，文中提到需要数千人的样本量来解析菌株多样性，但未提供具体的菌株水平跨队列分析数据。

## Optional bibliography

- [R1] 1.LarkinJFive-year survival with combined nivolumab and ipilimumab in advanced melanomaN. Engl. J. Med.20193811535154610.1056/NEJMoa191083631562797
- [R2] 2.AsciertoPASurvival outcomes in patients with previously untreated BRAF wild-type advanced melanoma treated with nivolumab therapy: three-year follow-up of a randomized phase 3 trialJAMA Oncol.2019518719410.1001/jamaoncol.2018.451430422243PMC6439558
- [R3] 3.LarkinJCombined nivolumab and ipilimumab or monotherapy in untreated melanomaN. Engl. J. Med.2015373233410.1056/NEJMoa150403026027431PMC5698905
- [R4] 4.AmariaRNNeoadjuvant immune checkpoint blockade in high-risk resectable melanomaNat. Med.2018241649165410.1038/s41591-018-0197-130297909PMC6481682
- [R5] 5.MatsonVThe commensal microbiome is associated with anti–PD-1 efficacy in metastatic melanoma patientsScience201835910410810.1126/science.aao329029302014PMC6707353
- [R6] 6.GopalakrishnanVGut microbiome modulates response to anti-PD-1 immunotherapy in melanoma patientsScience20183599710310.1126/science.aan423629097493PMC5827966
- [R7] 7.BaruchENFecal microbiota transplant promotes response in immunotherapy-refractory melanoma patientsScience20203760260910.1126/science.abb592033303685
- [R8] 8.McQuadeJLGut microbiome modulation via fecal microbiota transplant to augment immunotherapy in patients with melanoma or other cancers.Curr. Oncol. Rep.2020227410.1007/s11912-020-00913-y32577835PMC7685568
- [R9] 9.DubinKIntestinal microbiome analyses identify melanoma patients at risk for checkpoint-blockade-induced colitisNat. Commun.201671039110.1038/ncomms1039126837003PMC4740747
- [R10] 10.VétizouMAnticancer immunotherapy by CTLA-4 blockade relies on the gut microbiotaScience20153501079108410.1126/science.aad132926541610PMC4721659
- [R11] 11.RoutyBGut microbiome influences efficacy of PD-1-based immunotherapy against epithelial tumorsScience2018359919710.1126/science.aan370629097494
- [R12] 12.FrankelAEMetagenomic shotgun sequencing and unbiased metabolomic profiling identify specific human gut microbiota and metabolites associated with immune checkpoint therapy efficacy in melanoma patientsNeoplasia20171984885510.1016/j.neo.2017.08.00428923537PMC5602478
- [R13] 13.WirbelJMeta-analysis of fecal metagenomes reveals global microbial signatures that are specific for colorectal cancerNat. Med.20192567968910.1038/s41591-019-0406-630936547PMC7984229
- [R14] 14.ThomasAMMetagenomic analysis of colorectal cancer datasets identifies cross-cohort microbial diagnostic signatures and a link with choline degradationNat. Med.20192566767810.1038/s41591-019-0405-730936548PMC9533319
- [R15] 15.BeghiniFIntegrating taxonomic, functional, and strain-level profiling of diverse microbial communities with bioBakery 3.Elife202110e6508810.7554/eLife.6508833944776PMC8096432
- [R16] 16.WindTTGut microbial species and metabolic pathways associated with response to treatment with immune checkpoint inhibitors in metastatic melanomaMelanoma Res.20203023524610.1097/CMR.000000000000065631990790
- [R17] 17.PasolliETruongDTMalikFWaldronLSegataNMachine learning meta-analysis of large metagenomic datasets: Tools and biological insightsPLoS Comput. Biol.201612e100497710.1371/journal.pcbi.100497727400279PMC4939962
- [R18] 18.PetersBARelating the gut metagenome and metatranscriptome to immunotherapy responses in melanoma patientsGenome Med.2019116110.1186/s13073-019-0672-431597568PMC6785875
- [R19] 19.JohnsonWELiCRabinovicAAdjusting batch effects in microarray expression data using empirical Bayes methodsBiostatistics2007811812710.1093/biostatistics/kxj03716632515
- [R20] 20.AsnicarFMicrobiome connections with host metabolism and habitual diet from 1,098 deeply phenotyped individualsNat. Med.202110.1038/s41591-020-01183-833432175PMC8353542
- [R21] 21.ArtimoPExPASy: SIB bioinformatics resource portalNucleic Acids Res.201240W597W60310.1093/nar/gks40022661580PMC3394269
- [R22] 22.KanehisaMSatoYKawashimaMFurumichiMTanabeMKEGG as a reference resource for gene and protein annotationNucleic Acids Res.201644D457D46210.1093/nar/gkv107026476454PMC4702792
- [R23] 23.KettunenJBiomarker glycoprotein acetyls is associated with the risk of a wide spectrum of incident diseases and stratifies mortality risk in angiography patientsCirc. Genom. Precis. Med.201811e00223410.1161/CIRCGEN.118.00223430571186
- [R24] 24.HallABA novel Ruminococcus gnavus clade enriched in inflammatory bowel disease patientsGenome Med.2017910310.1186/s13073-017-0490-529183332PMC5704459
- [R25] 25.Valles-ColomerMThe neuroactive potential of the human gut microbiota in quality of life and depressionNat. Microbiol.2019462363210.1038/s41564-018-0337-x30718848
- [R26] 26.NiYHChuaH-HChouH-CCChiangB-LLiuH-HGut dysbiosis featured by abundant Ruminococcus gnavus heralds the manifestation of allergic diseases in infantsGastroenterology2017152S21410.1016/S0016-5085(17)31017-X28912020
- [R27] 27.RouxDIdentification of poly-N-acetylglucosamine as a major polysaccharide component of the Bacillus subtilis biofilm matrixJ. Biol. Chem.2015290192611927210.1074/jbc.M115.64870926078454PMC4521046
- [R28] 28.HeithoffDMSinsheimerRLLowDAMahanMJAn essential role for DNA adenine methylation in bacterial virulenceScience199928496797010.1126/science.284.5416.96710320378
- [R29] 29.TrogeAMore than a marine propeller: the flagellum of the probiotic Escherichia coli strain Nissle 1917 is the major adhesin mediating binding to human mucusInt. J. Med. Microbiol.201230230431410.1016/j.ijmm.2012.09.00423131416
- [R30] 30.ImhannFProton pump inhibitors affect the gut microbiomeGut20166574074810.1136/gutjnl-2015-31037626657899PMC4853569
- [R31] 31.MackeLSchulzCKoletzkoLMalfertheinerPSystematic review: the effects of proton pump inhibitors on the microbiome of the digestive tract-evidence from next-generation sequencing studiesAliment. Pharmacol. Ther.20205150552610.1111/apt.1560431990420
- [R32] 32.LlorenteCGastric acid suppression promotes alcoholic liver disease by inducing overgrowth of intestinal EnterococcusNat. Commun.2017883710.1038/s41467-017-00796-x29038503PMC5643518
- [R33] 33.AndrewsMCGut microbiota signatures are associated with toxicity to combined CTLA-4 and PD-1 blockadeNat. Med.2021271432144110.1038/s41591-021-01406-634239137PMC11107795
- [R34] 34.LimetaAMeta-analysis of the gut microbiota in predicting response to cancer immunotherapy in metastatic melanoma.JCI Insight20205e14094010.1172/jci.insight.140940PMC771440833268597
- [R35] 35.XieHShotgun metagenomics of 250 adult twins reveals genetic and environmental impacts on the gut microbiomeCell Syst.20163572584.e310.1016/j.cels.2016.10.00427818083PMC6309625
- [R36] 36.TruongDTTettAPasolliEHuttenhowerCSegataNMicrobial strain-level population structure and genetic diversity from metagenomesGenome Res.20172762663810.1101/gr.216242.11628167665PMC5378180
- [R37] 37.PasolliEExtensive unexplored human microbiome diversity revealed by over 150,000 genomes from metagenomes spanning age, geography, and lifestyleCell2019176649662.e2010.1016/j.cell.2019.01.00130661755PMC6349461
- [R38] 38.KarcherNAnalysis of 1321 Eubacterium rectale genomes from metagenomes uncovers complex phylogeographic population structure and subspecies functional adaptationsGenome Biol.20202113810.1186/s13059-020-02042-y32513234PMC7278147
- [R39] 39.van LeeLEvaluation of a screener to assess diet quality in the NetherlandsBr. J. Nutr.201611551752610.1017/S000711451500470526628073
- [R40] 40.BinghamSANutritional methods in the European Prospective Investigation of Cancer in NorfolkPublic Health Nutr.2001484785810.1079/PHN200010211415493
- [R41] 41.BinghamSAValidation of dietary assessment methods in the UK arm of EPIC using weighed records, and 24-hour urinary nitrogen and potassium and serum vitamin C and carotenoids as biomarkersInt. J. Epidemiol.199726S137S15110.1093/ije/26.suppl_1.S1379126542
- [R42] 42.TrichopoulouACostacouTBamiaCTrichopoulosDAdherence to a Mediterranean diet and survival in a Greek populationN. Engl. J. Med.20033482599260810.1056/NEJMoa02503912826634
- [R43] 43.SatijaAHealthful and unhealthful plant-based diets and the risk of coronary heart disease in U.S. adultsJ. Am. Coll. Cardiol.20177041142210.1016/j.jacc.2017.05.04728728684PMC5555375
- [R44] 44.LangmeadBSalzbergSLFast gapped-read alignment with Bowtie 2Nat. Methods2012935735910.1038/nmeth.192322388286PMC3322381
- [R45] 45.QuinceCWalkerAWSimpsonJTLomanNJSegataNShotgun metagenomics, from sampling to analysisNat. Biotechnol.20173583384410.1038/nbt.393528898207
- [R46] 46.McIverLJbioBakery: a meta’omic analysis environmentBioinformatics2018341235123710.1093/bioinformatics/btx75429194469PMC6030947
- [R47] 47.TruongDTMetaPhlAn2 for enhanced metagenomic taxonomic profilingNat. Methods20151290290310.1038/nmeth.358926418763
- [R48] 48.FranzosaEASpecies-level functional profiling of metagenomes and metatranscriptomesNat. Methods20181596296810.1038/s41592-018-0176-y30377376PMC6235447
- [R49] 49.BeghiniFLarge-scale comparative metagenomics of Blastocystis, a common member of the human gut microbiomeISME J.2017112848286310.1038/ismej.2017.13928837129PMC5702742
- [R50] 50.BristerJRAko-AdjeiDBaoYBlinkovaONCBI viral genomes resourceNucleic Acids Res.201543D571D57710.1093/nar/gku120725428358PMC4383986
- [R51] 51.ZolfoMDetecting contamination in viromes using ViromeQCNat. Biotechnol.2019371408141210.1038/s41587-019-0334-531748692
- [R52] 52.LinHPeddadaSDAnalysis of compositions of microbiomes with bias correctionNat. Commun.202011351410.1038/s41467-020-17041-732665548PMC7360769
- [R53] 53.Silverman, J. D., Roche, K., Holmes, Z. C., David, L. A. & Mukherjee, S. Bayesian multinomial logistic normal models through marginally latent Matrix-T processes. Preprint at https://arxiv.org/abs/1903.11695 (2019).
- [R54] 54.AtchisonJShenSMLogistic-normal distributions: some properties and usesBiometrika19806726127210.1093/biomet/67.2.261
- [R55] 55.MortonJTEstablishing microbial composition measurement standards with reference framesNat. Commun.201910271910.1038/s41467-019-10656-531222023PMC6586903
- [R56] 56.CalgaroMRomualdiCWaldronLRissoDVituloNAssessment of statistical methods from single cell, bulk RNA-seq, and metagenomics applied to microbiome dataGenome Biol.20202119110.1186/s13059-020-02104-132746888PMC7398076
- [R57] 57.LoveMIHuberWAndersSModerated estimation of fold change and dispersion for RNA-seq data with DESeq2Genome Biol.20141555010.1186/s13059-014-0550-825516281PMC4302049
- [R58] 58.RissoDPerraudeauFGribkovaSDudoitSVertJ-PA general and flexible method for signal extraction from single-cell RNA-seq dataNat. Commun.2018928410.1038/s41467-017-02554-529348443PMC5773593
- [R59] 59.RobinsonMDMcCarthyDJSmythGKedgeR: a Bioconductor package for differential expression analysis of digital gene expression dataBioinformatics20102613914010.1093/bioinformatics/btp61619910308PMC2796818
- [R60] 60.RitchieMElimma powers differential expression analyses for RNA-sequencing and microarray studiesNucleic Acids Res.201543e4710.1093/nar/gkv00725605792PMC4402510
- [R61] 61.MallickHMultivariable association discovery in population-scale meta-omics studiesPLoS Comput. Biol.202117e100944210.1371/journal.pcbi.100944234784344PMC8714082
- [R62] 62.WirbelJMicrobiome meta-analysis and cross-disease comparison enabled by the SIAMCAT machine learning toolboxGenome Biol.2021229310.1186/s13059-021-02306-133785070PMC8008609