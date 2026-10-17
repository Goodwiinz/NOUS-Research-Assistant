# Remaining Issues Breakdown

> **Historical, not current status (2026-10-17): this tracker records the state of one past fix session. Its percentages, checkboxes and remaining-issue lists are not maintained and must not be read as the current state of the code.**

## Summary Statistics
- **Total Issues Provided**: 70+
- **Issues Fixed This Session**: 8
- **Remaining Issues**: 62+
- **Estimated Remaining Time**: 4-6 hours

## Issues by Category

### 1. Frontend Evaluation Components (45 issues)

#### AutomatedEvaluationScheduling.tsx (5 issues)
- [ ] Add `null` to nextRun type declaration (lines 114-115)
- [ ] Validate dayOfWeek bounds before array indexing (lines 872-877)
- [ ] Fix status/success inconsistency in mock data (lines 602-625)
- [ ] Validate channels input with explicit validation (lines 1221-1227)
- [ ] Add onScheduleUpdate callback invocation (lines 644-694)

#### AutomatedQualityChecks.tsx (3 issues)
- [ ] Fix schedule toggle to use metadata.updated (lines 1336-1349)
- [ ] Fix averageScore divide-by-zero (lines 760-770)
- [ ] Fix update branch to use metadata.updated (lines 952-960)

#### BenchmarkComparisonTools.tsx (4 issues)
- [ ] Move Props interface before component (lines 1010-1023)
- [ ] Add missing Settings and ChevronRight icons (lines 45-69)
- [ ] Fix rank direction for lower-is-better metrics (lines 311-318)
- [ ] Normalize radar chart metrics to 0-100 scale (lines 820-856)

#### CustomMetricCreationTracking.tsx (3 issues)
- [ ] Replace Function constructor with safe evaluator (lines 358-386) **SECURITY CRITICAL**
- [ ] Add selectedDataSources multi-select UI (lines 916-932, 1072)
- [ ] Add onMetricUpdate callback invocation (lines 413-451)

#### DetailedAnalyticsReports.tsx (5 issues)
- [ ] Export ReportConfig interface (lines 90-113)
- [ ] Add useEffect cleanup for setTimeout (lines 404-410)
- [ ] Fix modalityDistribution percentages (lines 263-271)
- [ ] Type onExportReport/onShareReport properly (lines 941-943)
- [ ] Add overview TabsContent (lines 608-615)

#### EvaluationDataExportArchival.tsx (1 issue)
- [ ] Remove stray tilde character (lines 993-996)

#### HumanEvaluationWorkflows.tsx (15 issues) **BLOCKING COMPONENT**
**Syntax Errors (Critical)**:
- [ ] Fix taskTypes object syntax error (lines 750-757)
- [ ] Fix max attribute to string "120" (lines 1712-1721)
- [ ] Fix max attribute to string "10" (lines 1737-1747)
- [ ] Fix malformed SelectItem JSX (lines 1877-1881)
- [ ] Fix max attribute with malformed value (lines 1906-1915)
- [ ] Fix interface type syntax error (lines 1947-1954)
- [ ] Remove duplicate export (line 1962)

**Field/Type Errors**:
- [ ] Add missing Shield, Globe, FileCheck icons (lines 55-94)
- [ ] Fix compensation.method 'per_hour' → 'hourly' (lines 544-546)
- [ ] Fix datetime-local to preserve full timestamp (lines 1561, 1815-1821)
- [ ] Fix formData.name → formData.title (lines 1643-1651)
- [ ] Fix template literal in compensation display (lines 1320-1329)
- [ ] Use title property consistently (lines 912-980, 1276, 1552)

**Chart Error**:
- [ ] Replace LineChart with ComposedChart for mixed Bar/Line (lines 1117-1133)

**CSS Error**:
- [ ] Fix "form-medium" → "font-medium" (line 1449)

#### PerformanceAlertingSystem.tsx (6 issues)
- [ ] Fix toggleRuleStatus to use metadata.updated (lines 593-599)
- [ ] Add parseInt radix for duration (line 1390)
- [ ] Fix handleSaveRule to use metadata.updated (lines 642-683)
- [ ] Add parseInt radix for evaluationWindow (line 1417)
- [ ] Add parseInt radix for cooldownPeriod (line 1476)
- [ ] Add onAlertRuleUpdate callback (lines 642-650)

#### QualityGovernanceCompliance.tsx (1 issue)
- [ ] Fix avgCompliance divide-by-zero (lines 426-433)

#### QualityImprovementRecommendations.tsx (2 issues)
- [ ] Fix avgConfidence divide-by-zero (lines 426-433)
- [ ] Fix percent calculation divide-by-zero (lines 623-636)

### 2. Frontend Other Components (3 issues)

#### SecurityComplianceChecker.tsx (2 issues)
- [ ] Update mock dates to future (lines 68-287)
- [ ] Add missing Target and ChevronRight icons (lines 9-29)

#### usePerformanceMonitoring.ts (1 issue)
- [ ] Implement actual render time measurement (lines 11-38)

### 3. Infrastructure & Security (14+ issues)

#### Docker Compose Security (5+ issues)
- [ ] Fix QDRANT_API_KEY exposed on command line
- [ ] Fix exposed Alertmanager port 9093
- [ ] Fix Redis URL missing password
- [ ] Fix ZAP scan error suppression with "|| true"
- [ ] Fix npm install supply-chain risk

#### Backup & Disaster Recovery Scripts (5+ issues)
- [ ] Fix SQL injection in database name
- [ ] Fix async SMTP operations
- [ ] Fix Qdrant restore placeholder
- [ ] Fix subprocess.run blocking calls
- [ ] Fix Neo4j/Qdrant sync operations

#### Deploy & Security Scripts (4+ issues)
- [ ] Fix blue/green deployment logic
- [ ] Fix YAML parsing issues
- [ ] Fix rollback logic
- [ ] Additional security script issues

## Priority Recommendations

### Immediate (Blocking Issues)
1. **HumanEvaluationWorkflows.tsx syntax errors** - Component won't compile
2. **CustomMetricCreationTracking.tsx Function constructor** - Security vulnerability

### High Priority (Security)
3. **Docker compose security issues** - Credential exposure
4. **Backup scripts SQL injection** - Database security
5. **Deploy scripts security** - Production deployment safety

### Medium Priority (Functionality)
6. **Evaluation components divide-by-zero** - Runtime errors
7. **Evaluation components type safety** - TypeScript errors
8. **Missing UI component features** - Incomplete functionality

### Low Priority (Nice-to-have)
9. **Mock date updates** - UI appearance
10. **Performance monitoring improvements** - Better metrics

## Files Requiring Attention (Ordered by Priority)

1. `frontend/src/components/evaluation/HumanEvaluationWorkflows.tsx` - **CRITICAL**
2. `frontend/src/components/evaluation/CustomMetricCreationTracking.tsx` - **SECURITY**
3. `docker-compose.yml` - **SECURITY**
4. `scripts/backup_disaster_recovery.py` - **SECURITY**
5. `scripts/deploy.sh` - **SECURITY**
6. `frontend/src/components/evaluation/PerformanceAlertingSystem.tsx`
7. `frontend/src/components/evaluation/AutomatedEvaluationScheduling.tsx`
8. `frontend/src/components/evaluation/DetailedAnalyticsReports.tsx`
9. `frontend/src/components/evaluation/BenchmarkComparisonTools.tsx`
10. `frontend/src/components/evaluation/AutomatedQualityChecks.tsx`
11. `frontend/src/components/evaluation/QualityImprovementRecommendations.tsx`
12. `frontend/src/components/evaluation/QualityGovernanceCompliance.tsx`
13. `frontend/src/components/evaluation/EvaluationDataExportArchival.tsx`
14. `frontend/src/components/security/SecurityComplianceChecker.tsx`
15. `frontend/src/hooks/usePerformanceMonitoring.ts`

## Suggested Next Session Plan

### Session 4 (Estimated 2 hours)
- Fix HumanEvaluationWorkflows.tsx (all 15 issues)
- Fix CustomMetricCreationTracking.tsx security issue
- Fix Docker compose security issues

### Session 5 (Estimated 1.5 hours)
- Fix backup/disaster recovery scripts
- Fix deploy scripts
- Fix remaining PerformanceAlertingSystem.tsx issues

### Session 6 (Estimated 1.5 hours)
- Fix AutomatedEvaluationScheduling.tsx
- Fix DetailedAnalyticsReports.tsx
- Fix BenchmarkComparisonTools.tsx

### Session 7 (Estimated 1 hour)
- Fix remaining evaluation components
- Fix SecurityComplianceChecker.tsx
- Fix usePerformanceMonitoring.ts
- Final testing and validation

## Notes for Next Developer

1. **HumanEvaluationWorkflows.tsx** has the most issues (15) and is blocking - prioritize this
2. Several components have **metadata.updated** vs root **updated** field confusion - pattern is consistent
3. Many components need **divide-by-zero guards** - can be done in batch
4. **parseInt** calls need radix (10) parameter - easy regex find/replace
5. **Icons** need to be added to import statements - check lucide-react docs
6. **Type safety** improvements may require creating/exporting new interfaces
7. Some **mock data** dates are in the past - update to relative dates like we did for ABTestingQueryImprovements

## Testing Strategy

After fixing issues:

1. **Compile Check**: Run `npm run type-check` in frontend
2. **Lint Check**: Run `npm run lint` in frontend
3. **Unit Tests**: Run existing test suites
4. **Manual Testing**: 
   - Test evaluation components with edge cases (zero values, null data)
   - Test workflow components with various user inputs
   - Verify security configurations don't expose credentials
5. **Security Scan**: Run `npm audit` and security scanners on fixed code
