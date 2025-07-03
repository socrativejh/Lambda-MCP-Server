from awslabs.mcp_lambda_handler import MCPLambdaHandler
from datetime import datetime, UTC
import random
import boto3
import os
import ipaddress
import json
import ast

# Get session table name from environment variable
session_table = os.environ.get('MCP_SESSION_TABLE', 'mcp_sessions')

# Create the MCP server instance
mcp_server = MCPLambdaHandler(name="mcp-lambda-server", version="1.0.0", session_store=session_table)

@mcp_server.tool()
def get_weather(city: str) -> str:
    """Get the current weather for a city.
    
    Args:
        city: Name of the city to get weather for
        
    Returns:
        A string describing the weather
    """
    temp = random.randint(15, 35)
    return f"The temperature in {city} is {temp}°C"

@mcp_server.tool()
def count_s3_buckets() -> int:
    """Count the number of S3 buckets."""
    s3 = boto3.client('s3')
    response = s3.list_buckets()
    return len(response['Buckets'])

@mcp_server.tool()
def get_time() -> str:
    """Get the current UTC date and time, and show how long since last time was asked for (if session available)."""
    try:
        # Get the current UTC time as a datetime object and formatted string
        now = datetime.now(UTC)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")

        # Retrieve the session object if available
        session = mcp_server.get_session()
        # Get the last time the user asked for the time from the session (if any)
        last_time = session.get('last_time_asked') if session else None
        last_time_str = last_time if last_time else None
        seconds_since = None

        # If there was a previous time, calculate how many seconds have passed
        if last_time:
            try:
                last_time_dt = datetime.strptime(last_time, "%Y-%m-%d %H:%M:%S")
                seconds_since = int((now - last_time_dt).total_seconds())
            except Exception:
                # If parsing fails, just skip the seconds_since calculation
                seconds_since = None

        # Store the current time as the new 'last_time_asked' in the session
        if session:
            def update_last_time(s):
                s.set('last_time_asked', now_str)
            mcp_server.update_session(update_last_time)

        # Build the response string
        response = f"Current UTC time: {now_str}"
        if last_time_str:
            response += f"\nLast time asked: {last_time_str}"
            if seconds_since is not None:
                response += f"\nSeconds since last time: {seconds_since}"
        return response
    
    except Exception as e:
        # Catch-all for unexpected errors
        return f"Error: An unexpected error occurred: {str(e)}"

@mcp_server.tool()
def create_vpc(params: dict) -> dict:
    """
    Generalized VPC and Subnet Creator for OSS contribution.

    Parameters expected in `params`:
    - cidr_block: str
    - az_list: list[str]
    - public_subnets_per_az: int
    - private_subnets_per_az: int
    - name_tag: str

    e.g.
    createVpc(params={"cidr_block":"10.20.0.0/16","az_list":["us-west-2a","us-west-2b"],"public_subnets_per_az":1,"private_subnets_per_az":1,"name_tag":"mcp"})
    """
    ec2 = boto3.client("ec2", region_name="us-west-2")
    out = {}


    if isinstance(params, str):
        params = json.loads(params)
    # Extract parameters with defaults
    cidr_block = params.get("cidr_block", "10.0.0.0/16")
    az_list = params.get("az_list", ["us-west-2a", "us-west-2b"])
    public_subnets_per_az = params.get("public_subnets_per_az", 1)
    private_subnets_per_az = params.get("private_subnets_per_az", 1)
    name_tag = params.get("name_tag", "mcp-server")

    total_subnets_needed = (public_subnets_per_az + private_subnets_per_az) * len(az_list)
    subnet_blocks = list(ipaddress.ip_network(cidr_block).subnets(new_prefix=24))
    if len(subnet_blocks) < total_subnets_needed:
        raise Exception("Not enough subnet blocks in CIDR for requested subnets.")

    # Check existing VPC
    existing = ec2.describe_vpcs(Filters=[
        {"Name": "tag:Name", "Values": [f"{name_tag}-vpc"]}
    ])["Vpcs"]
    if existing:
        vpc_id = existing[0]["VpcId"]
        print(f"[INFO] Existing VPC found: {vpc_id}")
        out["VpcId"] = vpc_id
        return out

    # Create VPC
    vpc_id = ec2.create_vpc(CidrBlock=cidr_block)["Vpc"]["VpcId"]
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsSupport={"Value": True})
    ec2.modify_vpc_attribute(VpcId=vpc_id, EnableDnsHostnames={"Value": True})
    ec2.create_tags(Resources=[vpc_id],
                    Tags=[{"Key": "Name", "Value": f"{name_tag}-vpc"}])
    out["VpcId"] = vpc_id

    # IGW and main route
    igw_id = ec2.create_internet_gateway()["InternetGateway"]["InternetGatewayId"]
    ec2.attach_internet_gateway(VpcId=vpc_id, InternetGatewayId=igw_id)
    out["InternetGatewayId"] = igw_id

    main_rt_id = ec2.describe_route_tables(
        Filters=[
            {"Name": "vpc-id", "Values": [vpc_id]},
            {"Name": "association.main", "Values": ["true"]}
        ]
    )["RouteTables"][0]["RouteTableId"]
    ec2.create_route(RouteTableId=main_rt_id,
                     DestinationCidrBlock="0.0.0.0/0",
                     GatewayId=igw_id)

    out["PublicSubnets"] = []
    out["PrivateSubnets"] = []

    idx = 0
    for az in az_list:
        # Create public subnets
        for i in range(public_subnets_per_az):
            cidr_pub = str(subnet_blocks[idx])
            idx += 1
            pub_subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr_pub, AvailabilityZone=az,
                TagSpecifications=[{
                    "ResourceType": "subnet",
                    "Tags": [{"Key": "Name", "Value": f"{name_tag}-pub-{az}-{i}"}]
                }]
            )["Subnet"]
            pub_id = pub_subnet["SubnetId"]
            ec2.modify_subnet_attribute(SubnetId=pub_id, MapPublicIpOnLaunch={"Value": True})
            out["PublicSubnets"].append(pub_id)

        # Create NAT Gateway for private subnets
        eip = ec2.allocate_address(Domain="vpc")["AllocationId"]
        nat = ec2.create_nat_gateway(
            SubnetId=pub_id, AllocationId=eip,
            TagSpecifications=[{
                "ResourceType": "natgateway",
                "Tags": [{"Key": "Name", "Value": f"{name_tag}-nat-{az}"}]
            }]
        )["NatGateway"]
        nat_id = nat["NatGatewayId"]

        waiter = ec2.get_waiter("nat_gateway_available")
        print(f"Waiting for NAT Gateway {nat_id} in {az}...")
        waiter.wait(NatGatewayIds=[nat_id])
        print(f"NAT Gateway {nat_id} is available.")

        # Create private subnets
        for i in range(private_subnets_per_az):
            cidr_pri = str(subnet_blocks[idx])
            idx += 1
            pri_subnet = ec2.create_subnet(
                VpcId=vpc_id, CidrBlock=cidr_pri, AvailabilityZone=az,
                TagSpecifications=[{
                    "ResourceType": "subnet",
                    "Tags": [{"Key": "Name", "Value": f"{name_tag}-pri-{az}-{i}"}]
                }]
            )["Subnet"]
            pri_id = pri_subnet["SubnetId"]
            out["PrivateSubnets"].append(pri_id)

            rt_priv = ec2.create_route_table(VpcId=vpc_id)["RouteTable"]["RouteTableId"]
            ec2.associate_route_table(RouteTableId=rt_priv, SubnetId=pri_id)
            ec2.create_route(RouteTableId=rt_priv,
                             DestinationCidrBlock="0.0.0.0/0",
                             NatGatewayId=nat_id)

    return {
        "Message": f"VPC {vpc_id} and related resources created successfully."
    }

def lambda_handler(event, context):
    """AWS Lambda handler function."""
    return mcp_server.handle_request(event, context) 