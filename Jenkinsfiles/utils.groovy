/*
 * Define utility functions.
 */

def checkout_github(repo, branch, relative_target_dir) {
    checkout(
        [$class: 'GitSCM',
         userRemoteConfigs: [[url: "https://github.com/${repo}"]],
         branches: [[name: "refs/heads/${branch}"]],
         extensions: [[$class: 'RelativeTargetDirectory',
                       relativeTargetDir: relative_target_dir]],
         doGenerateSubmoduleConfigurations: false,
         submoduleCfg: []]
    )
}

def notify_irc(Map args) {
    def command = "scripts/irc-notify.sh"
    for (arg in args) {
        command += " --${arg.key} '${arg.value}'"
    }
    sh command
}

def make(setup, cmd) {
    sh """
        . ${setup}
        make ${cmd} KUMA_IMAGE_TAG=0c60774
    """
}

def make_stage(cmd) {
    make('regions/portland/stage.sh', cmd)
}

def migrate_stage_db() {
    make_stage('k8s-db-migration-job')
    notify_irc([
        irc_nick: 'mdnstagepush',
        stage: 'Start DB Migration',
        status: 'success'
    ])
}

def deploy_kuma_to_stage() {
    make_stage('k8s-kuma-deployments')
    notify_irc([
        irc_nick: 'mdnstagepush',
        stage: 'Start Kuma Rollout',
        status: 'success'
    ])
}

def monitor_deploy_kuma_to_stage() {
    make_stage('k8s-kuma-rollout-status')
    notify_irc([
        irc_nick: 'mdnstagepush',
        stage: 'Kuma Rollout Completed',
        status: 'success'
    ])
}

return this;
