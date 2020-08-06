from pulumi import Config, export, get_project, get_stack, Output, ResourceOptions
import pulumi
import pulumi_gcp as gcp
from pulumi_gcp.config import project, zone
from pulumi_gcp.container import Cluster, get_engine_versions
from pulumi_kubernetes import Provider
from pulumi_kubernetes.apps.v1 import Deployment
from pulumi_kubernetes.core.v1 import Service
from pulumi_random import RandomPassword
from pulumi_kubernetes.helm.v3 import Chart, ChartOpts, FetchOpts, LocalChartOpts
import pulumi_kafka as kafka
from pulumi_kubernetes.rbac.v1 import ClusterRoleBinding
from pulumi_kubernetes.yaml import ConfigFile
from pulumi_kubernetes.core.v1 import Service, Namespace
from pulumi_gcp.container import Cluster, get_engine_versions, NodePool
from pulumi_kubernetes.networking.v1beta1 import Ingress
from pulumi_kubernetes.core.v1 import PersistentVolumeClaim
from pulumi_kubernetes.batch.v1 import Job
from pulumi_kubernetes.core.v1 import Pod

# Read in some configurable settings for our cluster:
config = Config(None)

# nodeCount is the number of cluster nodes to provision. Defaults to 3 if unspecified.
NODE_COUNT = config.get('node_count') or 3
# nodeMachineType is the machine type to use for cluster nodes. Defaults to n1-standard-1 if unspecified.
# See https://cloud.google.com/compute/docs/machine-types for more details on available machine types.
NODE_MACHINE_TYPE = config.get('node_machine_type') or 'n1-standard-1'
# username is the admin username for the cluster.
USERNAME = config.get('username') or 'admin'
# password is the password for the admin user in the cluster.
PASSWORD = config.get_secret('password') or RandomPassword("password", length=20, special=True).result
# master version of GKE engine
MASTER_VERSION = config.get('master_version')
# Retrieve email
me = gcp.organizations.get_client_open_id_user_info()
export("my-email", me.email)

# Now, actually create the GKE cluster.
k8s_cluster = Cluster('gke-cluster',
    initial_node_count=NODE_COUNT,
    node_version=MASTER_VERSION,
    min_master_version=MASTER_VERSION,
    master_auth={ 'username': USERNAME, 'password': PASSWORD },
    node_config={
        'machine_type': NODE_MACHINE_TYPE,
        'oauth_scopes': [
            'https://www.googleapis.com/auth/compute',
            'https://www.googleapis.com/auth/devstorage.read_only',
            'https://www.googleapis.com/auth/logging.write',
            'https://www.googleapis.com/auth/monitoring'
        ],
    },
)

k8s_nodepool=NodePool("demo-k8s-cluster-nodepool", 
    autoscaling={
          "maxNodeCount":16, 
          "minNodeCount":1,
    },
    location=zone,
    cluster=k8s_cluster.name,
    node_count=NODE_COUNT,
    name="demo-k8s-node",
    management={
        "autoRepair":True,
        "autoUpgrade":False,
    },
    #opts=ResourceOptions(depends_on=[k8s_cluster], delete_before_replace=True),  
)

# Manufacture a GKE-style Kubeconfig. Note that this is slightly "different" because of the way GKE requires
# gcloud to be in the picture for cluster authentication (rather than using the client cert/key directly).
k8s_info = Output.all(k8s_cluster.name, k8s_cluster.endpoint, k8s_cluster.master_auth)
k8s_config = k8s_info.apply(
    lambda info: """apiVersion: v1
clusters:
- cluster:
    certificate-authority-data: {0}
    server: https://{1}
  name: {2}
contexts:
- context:
    cluster: {2}
    user: {2}
  name: {2}
current-context: {2}
kind: Config
preferences: {{}}
users:
- name: {2}
  user:
    auth-provider:
      config:
        cmd-args: config config-helper --format=json
        cmd-path: gcloud
        expiry-key: '{{.credential.token_expiry}}'
        token-key: '{{.credential.access_token}}'
      name: gcp
""".format(info[2]['clusterCaCertificate'], info[1], '{0}_{1}_{2}'.format(project, zone, info[0])))

# Make a Kubernetes provider instance that uses our cluster from above.
k8s_provider = Provider('gke_k8s', kubeconfig=k8s_config)

crb = ClusterRoleBinding("cluster-admin-binding", 
    metadata={ 'name': 'cluster-admin-binding' },
    role_ref={
        "api_group":"rbac.authorization.k8s.io", 
        "kind":"ClusterRole",
        "name":"cluster-admin",
    }, 
    subjects=[{
        "kind":"User", 
        "name":gcp.organizations.get_client_open_id_user_info().email
        },],
    #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster],),
    opts=ResourceOptions(provider=k8s_provider)
)

# system_namespace = Namespace("hooli-k8s-system", 
#     #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool]),
# )

# cert_manager = ConfigFile('cert-manager', 'https://github.com/jetstack/cert-manager/releases/download/v0.15.1/cert-manager-legacy.yaml')
# cert_manager = ConfigFile('cert-manager', 'https://github.com/jetstack/cert-manager/releases/download/v0.16.0/cert-manager.yaml')

# cert_issuer = ConfigFile('vert-issuer', './yaml/cluster-issuer.yaml')

# Create Helm Chart
nginx_ingress = Chart("demo-nginx-ingress", 
        config=ChartOpts(
            chart="nginx-ingress",
            #namespace=system_namespace.id,
            fetch_opts=FetchOpts(
                  repo="https://kubernetes-charts.storage.googleapis.com/",
            ),
            values={
                "controller": {
                    "metrics": {
                        "enabled": True,
                    },
                },
            },
        ),
        #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
)

# Deploy Spring Cloud Data Flow using Helm Chart
scdf = Chart("jc-scdf", 
        config=ChartOpts(
            chart="spring-cloud-data-flow",
            #namespace=system_namespace.id,
            #path="./spring-cloud-data-flow",
            fetch_opts=FetchOpts(
                   repo="https://kubernetes-charts.storage.googleapis.com/",
             ),
            values={
                "kafka": {
                    "enabled":True,
                    "persistence": {"size":"20Gi",},
                },
                "rabbitmq": {"enabled":False,},
                "features": {"monitoring": {"enabled":True,},},
                "server": {"service": {"type":"ClusterIP",},},
                "grafana":{"service":{"type":"ClusterIP",},},
                "prometheus":{"proxy":{"service":{"type":"ClusterIP",},},},
                "ingress":{
                    "enabled":True,
                    "protocol":"http",
                },
            },
        ),
        #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
)

# # data_flow_config=ConfigFile('data-flow-config', './yaml/scdf-dashboard-ingress.yaml')
data_flow_ingress = Ingress("data-flow-ingress", 
    metadata={
        'name':'scdf-dashboard-ingress',
        'annotations':{
            "kubernetes.io/ingress.class": "nginx",
            "cert-manager.io/cluster-issuer": "letsencrypt-staging"
#            "cert-manager.io/cluster-issuer": "letsencrypt-prod"
        },
    },
    spec={
        'rules': [{
            'host': "scdf.jc.paradymelabs.com",
            'http': {
                'paths': [{
                    "path": "/",
                    "backend": {
                        "serviceName": "jc-scdf-data-flow-server",
                        "servicePort": 80,
                    }
                },],
            },
        },],
        'tls': [{
             'hosts': ["scdf.jc.paradymelabs.com",],
             'secretName':'dataflow-certificate',
        },],
    },
    #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
    opts=ResourceOptions(provider=k8s_provider)
)

#prometheus_config=ConfigFile('prometheus-config', './yaml/prometheus-ingress.yaml')
prometheus_ingress = Ingress("prometheus-ingress", 
    metadata={
        'name':'prometheus-dashboard-ingress',
        'annotations':{
            "kubernetes.io/ingress.class": "nginx",
            "cert-manager.io/cluster-issuer": "letsencrypt-staging"
#            "cert-manager.io/cluster-issuer": "letsencrypt-prod"
        },
    },
    spec={
        'rules': [{
            'host': "prometheus.jc.paradymelabs.com",
            'http': {
                'paths': [{
                    "path": "/",
                    "backend": {
                        "serviceName": "jc-scdf-prometheus-server",
                        "servicePort": 80,
                    }
                },],
            },
        },],
        'tls': [{
             'hosts': ["prometheus.jc.paradymelabs.com",],
             'secretName':'prometheus-certificate',
        },],
    },
    #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
    opts=ResourceOptions(provider=k8s_provider)
)

#grafana_config=ConfigFile('grafana-config', './yaml/grafana-ingress.yaml')
grafana_ingress = Ingress("grafana-ingress", 
    metadata={
        'name':'grafana-dashboard-ingress',
        'annotations':{
            "kubernetes.io/ingress.class": "nginx",
            "cert-manager.io/cluster-issuer": "letsencrypt-staging"
#            "cert-manager.io/cluster-issuer": "letsencrypt-prod"
        },
    },
    spec={
        'rules': [{
            'host': "grafana.jc.paradymelabs.com",
            'http': {
                'paths': [{
                    "path": "/",
                    "backend": {
                        "serviceName": "jc-scdf-grafana-server",
                        "servicePort": 80,
                    }
                },],
            },
        },],
        'tls': [{
             'hosts': ["grafana.jc.paradymelabs.com",],
             'secretName':'grafana-certificate',
        },],
    },
    #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
    opts=ResourceOptions(provider=k8s_provider)
)

# # Change Grafana to use Persistent Storage
# # kubectl apply -f ./kubernetes/grafana-persistentvolumeclaim.yaml
grafana_storage = PersistentVolumeClaim("grafana-storage",
    metadata={
        'name':'grafana-storage',
    },
    spec={
        'accessModes': ['ReadWriteOnce',],
        'resources':{
            'requests': {'storage': '10Gi'},
        },
    },
    #opts=ResourceOptions(provider=k8s_provider, depends_on=[k8s_cluster, k8s_nodepool, system_namespace], delete_before_replace=True),
    opts=ResourceOptions(provider=k8s_provider)
)

#kubectl patch deployment jedi-pipes-grafana --patch "$(cat ./kubernetes/grafana-deployment.yaml)"

# # Finally, export the kubeconfig so that the client can easily access the cluster.
export('kubeconfig', k8s_config)
# # Export the k8s ingress IP to access the canary deployment
# export('ingress_ip', Output.all(ingress.status['load_balancer']['ingress'][0]['ip']))
# export('ingress_ip', Output.all(nginx_ingress.status['load_balancer']['ingress'][0]['ip']))