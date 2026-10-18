// GPL-3.0; Komodo Operation enum snapshot. See NOTICE.md for source revision.
pub enum Operation {
  // do nothing
  #[default]
  None,

  // Swarm
  CreateSwarm,
  UpdateSwarm,
  RenameSwarm,
  DeleteSwarm,
  RemoveSwarmNodes,
  UpdateSwarmNode,
  RemoveSwarmStacks,
  RemoveSwarmServices,
  CreateSwarmConfig,
  RotateSwarmConfig,
  RemoveSwarmConfigs,
  CreateSwarmSecret,
  RotateSwarmSecret,
  RemoveSwarmSecrets,

  // Server
  CreateServer,
  UpdateServer,
  UpdateServerKey,
  DeleteServer,
  RenameServer,
  StartContainer,
  RestartContainer,
  PauseContainer,
  UnpauseContainer,
  StopContainer,
  DestroyContainer,
  StartAllContainers,
  RestartAllContainers,
  PauseAllContainers,
  UnpauseAllContainers,
  StopAllContainers,
  PruneContainers,
  CreateNetwork,
  DeleteNetwork,
  PruneNetworks,
  DeleteImage,
  PruneImages,
  DeleteVolume,
  PruneVolumes,
  PruneDockerBuilders,
  PruneBuildx,
  PruneSystem,

  // Stack
  CreateStack,
  UpdateStack,
  RenameStack,
  DeleteStack,
  WriteStackContents,
  RefreshStackCache,
  PullStack,
  DeployStack,
  StartStack,
  RestartStack,
  PauseStack,
  UnpauseStack,
  StopStack,
  DestroyStack,
  RunStackService,
  CheckStackForUpdate,

  // Stack (Service)
  DeployStackService,
  PullStackService,
  StartStackService,
  RestartStackService,
  PauseStackService,
  UnpauseStackService,
  StopStackService,
  DestroyStackService,

  // Deployment
  CreateDeployment,
  UpdateDeployment,
  RenameDeployment,
  DeleteDeployment,
  Deploy,
  PullDeployment,
  StartDeployment,
  RestartDeployment,
  PauseDeployment,
  UnpauseDeployment,
  StopDeployment,
  DestroyDeployment,
  CheckDeploymentForUpdate,

  // Build
  CreateBuild,
  UpdateBuild,
  RenameBuild,
  DeleteBuild,
  RunBuild,
  CancelBuild,
  WriteDockerfile,

  // Repo
  CreateRepo,
  UpdateRepo,
  RenameRepo,
  DeleteRepo,
  CloneRepo,
  PullRepo,
  BuildRepo,
  CancelRepoBuild,

  // Procedure
  CreateProcedure,
  UpdateProcedure,
  RenameProcedure,
  DeleteProcedure,
  RunProcedure,
  CancelProcedure,

  // Action
  CreateAction,
  UpdateAction,
  RenameAction,
  DeleteAction,
  RunAction,
  CancelAction,

  // Sync
  CreateResourceSync,
  UpdateResourceSync,
  RenameResourceSync,
  DeleteResourceSync,
  WriteSyncContents,
  CommitSync,
  RunSync,

  // Builder
  CreateBuilder,
  UpdateBuilder,
  RenameBuilder,
  DeleteBuilder,

  // Alerter
  CreateAlerter,
  UpdateAlerter,
  RenameAlerter,
  DeleteAlerter,
  TestAlerter,
  SendAlert,

  // Maintenance
  ClearRepoCache,
  BackupCoreDatabase,
  GlobalAutoUpdate,
  RotateAllServerKeys,
  RotateCoreKeys,

  // Variable
  CreateVariable,
  UpdateVariableValue,
  DeleteVariable,

  // Git Provider
  CreateGitProviderAccount,
  UpdateGitProviderAccount,
  DeleteGitProviderAccount,

  // Docker Registry
  CreateDockerRegistryAccount,
  UpdateDockerRegistryAccount,
  DeleteDockerRegistryAccount,
}
